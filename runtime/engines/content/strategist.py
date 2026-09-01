"""
runtime/engines/content/strategist.py

Content Strategist Engine — Agent 3.

Responsabilidad:
    Recibe una Opportunity priorizada (Agent 2) y el contexto de la Mission,
    y genera un ContentBrief validado listo para el Agent 4.

Garantías:
    - Usa ÚNICAMENTE el LLMProvider inyectado (NO hardcodea OpenAI).
    - La respuesta del LLM es validada mediante Pydantic (Structured Output).
    - Errores semánticos (JSON roto, schema inválido, campos faltantes) lanzan
      InvalidContentBriefOutputError — NO provocan rollback del Agente 2.
    - Errores de provider/red (LLMProviderError, RuntimeError) se propagan
      como están — son retryables según la arquitectura del worker.

Separación de responsabilidades:
    ContentStrategist:  lógica de generación (prompt + parseo + validación).
    run_content_strategy_flow(): persistencia + aislamiento transaccional.
"""

import json
import logging

from pydantic import BaseModel, ConfigDict, ValidationError

from runtime.contracts.content_brief import (
    ContentAngle,
    ContentBrief,
    ContentBriefSection,
    ContentFormat,
    ContentObjective,
)
from runtime.contracts.knowledge import Opportunity
from runtime.infrastructure.llm.interfaces import LLMProvider
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Excepción semántica específica del Agent 3
# ---------------------------------------------------------------------------


class InvalidContentBriefOutputError(ValueError):
    """
    Error semántico controlado del Content Strategist (Agent 3).

    Se lanza ÚNICAMENTE cuando el LLM responde con HTTP 200 pero:
      - El JSON está roto o no es parseable.
      - El schema es inválido (enum desconocido, tipo incorrecto).
      - Faltan campos obligatorios (hook, sections vacío, etc.).

    NO aplica a:
      - Timeout / error de autenticación / error de red (→ propagados como están).
      - Errores de base de datos (→ SQLAlchemy exceptions, fatales).

    Garantía: capturado en run_content_strategy_flow() → Opportunity intacta.
    """


# ---------------------------------------------------------------------------
# Modelo privado para validar la respuesta JSON del LLM
# ---------------------------------------------------------------------------


class _LLMContentBriefRaw(BaseModel):
    """Schema estricto para la respuesta JSON del LLM."""

    model_config = ConfigDict(populate_by_name=True)

    content_format: ContentFormat
    objective: ContentObjective
    target_audience: str
    angle: ContentAngle
    core_message: str
    hook: str
    sections: list[dict]  # Validados individualmente abajo
    cta: str
    visual_direction: str
    source_reasoning: str


# ---------------------------------------------------------------------------
# ContentStrategist
# ---------------------------------------------------------------------------


class ContentStrategist:
    """
    Motor LLM del Agent 3.

    Genera un ContentBrief a partir de una Opportunity y el contexto de misión.
    Usa Structured Output validado por Pydantic para garantizar conformidad al schema.

    Args:
        llm_provider: Implementación de LLMProvider (inyectada, no hardcodeada).
    """

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    async def generate(
        self,
        opportunity: Opportunity,
        mission_context: str,
        observation_scope: str = "reference",
        brand_context: dict[str, str | list[str]] | None = None,
    ) -> ContentBrief:
        """
        Genera un ContentBrief validado a partir de una Opportunity.

        Args:
            opportunity:       Opportunity priorizada del Agent 2.
            mission_context:   Contexto de misión inyectado (nombre, scope, target).
            observation_scope: Propósito por el cual la fuente fue observada.
            brand_context:     Contexto de la marca para la cual se genera el contenido.

        Returns:
            ContentBrief validado y listo para persistencia.

        Raises:
            InvalidContentBriefOutputError: JSON roto, schema inválido, campos faltantes.
            LLMProviderError / RuntimeError: Fallos de infraestructura del provider.
        """
        prompt = self._build_prompt(opportunity, mission_context, observation_scope, brand_context)

        logger.info(
            "Content strategy generation started",
            extra={
                "opportunity_id": str(opportunity.id),
                "opportunity_title": opportunity.title,
                "priority": opportunity.priority,
            },
        )

        raw_response = await self._llm.complete(prompt)

        if not raw_response or not raw_response.strip():
            raise InvalidContentBriefOutputError(
                "El LLM devolvió una respuesta vacía al generar el ContentBrief."
            )

        brief = self._parse_and_validate(raw_response, opportunity)

        logger.info(
            "Content brief generated",
            extra={
                "opportunity_id": str(opportunity.id),
                "brief_id": str(brief.id),
                "content_format": brief.content_format.value,
                "objective": brief.objective.value,
            },
        )

        return brief

    def _parse_and_validate(
        self,
        raw_response: str,
        opportunity: Opportunity,
    ) -> ContentBrief:
        """
        Parsea y valida la respuesta JSON del LLM.

        Raises:
            InvalidContentBriefOutputError: Si el JSON es inválido o el schema falla.
        """
        try:
            # Limpiar markdown fences si el LLM los incluyó
            clean = raw_response.strip()
            if clean.startswith("```json"):
                clean = clean[7:]
            if clean.startswith("```"):
                clean = clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()

            parsed_data = json.loads(clean)
        except json.JSONDecodeError as e:
            raise InvalidContentBriefOutputError(
                f"El LLM devolvió JSON inválido en ContentBrief: {e}"
            ) from e

        try:
            raw = _LLMContentBriefRaw.model_validate(parsed_data)
        except ValidationError as e:
            raise InvalidContentBriefOutputError(
                f"La respuesta del LLM no cumple el schema de ContentBrief: {e}"
            ) from e

        # Validar y construir secciones
        sections = self._parse_sections(raw.sections)

        # Validar campos obligatorios mínimos
        if not raw.hook or not raw.hook.strip():
            raise InvalidContentBriefOutputError(
                "El campo 'hook' es obligatorio y no puede estar vacío."
            )
        if not sections:
            raise InvalidContentBriefOutputError(
                "El campo 'sections' debe contener al menos 1 sección."
            )

        try:
            return ContentBrief(
                mission_id=opportunity.mission_id,
                opportunity_id=opportunity.id,
                content_format=raw.content_format,
                objective=raw.objective,
                target_audience=raw.target_audience,
                angle=raw.angle,
                core_message=raw.core_message,
                hook=raw.hook,
                sections=sections,
                cta=raw.cta,
                visual_direction=raw.visual_direction,
                source_reasoning=raw.source_reasoning,
            )
        except (ValueError, ValidationError) as e:
            raise InvalidContentBriefOutputError(
                f"Error al construir el ContentBrief desde la respuesta del LLM: {e}"
            ) from e

    def _parse_sections(self, raw_sections: list[dict]) -> list[ContentBriefSection]:
        """
        Convierte y valida la lista de secciones crudas del JSON.

        Raises:
            InvalidContentBriefOutputError: Si alguna sección es inválida.
        """
        if not raw_sections:
            raise InvalidContentBriefOutputError(
                "El LLM no proveyó ninguna sección en 'sections'."
            )

        sections: list[ContentBriefSection] = []
        for i, raw_sec in enumerate(raw_sections):
            try:
                sec = ContentBriefSection.model_validate(raw_sec)
                sections.append(sec)
            except (ValidationError, TypeError) as e:
                raise InvalidContentBriefOutputError(
                    f"Sección {i} inválida en la respuesta del LLM: {e}"
                ) from e

        return sections

    def _build_prompt(
        self,
        opportunity: Opportunity,
        mission_context: str,
        observation_scope: str,
        brand_context: dict[str, str | list[str]] | None,
    ) -> str:
        """
        Construye el prompt para la generación del ContentBrief.

        El LLM debe responder con un JSON estricto que cumpla el schema.
        """
        formats = ", ".join(f.value for f in ContentFormat)
        objectives = ", ".join(o.value for o in ContentObjective)
        angles = ", ".join(a.value for a in ContentAngle)

        brand_info = ""
        if brand_context:
            services = ", ".join(brand_context.get("core_services", []))
            brand_info = f"""
Contexto de TU Marca (BRAND):
Eres el Content Strategist para {brand_context.get('brand_name', 'la marca')}.
Descripción: {brand_context.get('brand_description', '')}
Servicios: {services}
Audiencia: {brand_context.get('target_audience', '')}
Objetivo de contenido: {brand_context.get('content_goal', '')}
"""

        return f"""Eres RenderBrain Content Strategist — Agent 3.
Tu tarea es generar una propuesta de contenido concreta y accionable a partir de una Opportunity estratégica detectada.

{brand_info}

Contexto de la Misión (La FUENTE que observaste para aprender):
{mission_context}
Propósito de la observación (observation_scope): {observation_scope}

Opportunity a trabajar (Aprendizaje extraído de la fuente):
- Título: {opportunity.title}
- Descripción: {opportunity.description}
- Prioridad: {opportunity.priority}

Instrucciones:
1. Decide el FORMATO más adecuado para comunicar esta oportunidad: {formats}
2. Define el OBJETIVO comunicacional: {objectives}
3. Identifica la AUDIENCIA objetivo (descripción corta, 1-2 oraciones).
4. Elige el ÁNGULO narrativo: {angles}
5. Redacta el MENSAJE CENTRAL (core_message): la idea fundamental en 1 oración.
6. Escribe el HOOK: primera línea de apertura, corta, impactante, publicable directamente.
7. Desarrolla el BODY/SCRIPT como secciones ordenadas (sections). Mínimo 1 sección, máximo 5.
   - Para reel: guión por bloques narrativos.
   - Para carousel: un slide por sección.
   - Para static_post: una sola sección con el cuerpo completo.
8. Define el CTA coherente con el objetivo.
9. Da una DIRECCIÓN VISUAL conceptual (instrucción para el diseñador/productor, NO colores ni diseño final).
10. Redacta el SOURCE_REASONING: por qué esta pieza es la respuesta correcta a esta Opportunity. Sin chain-of-thought.

Reglas Críticas:
- You are creating content FOR {brand_context.get('brand_name', 'your brand') if brand_context else 'the brand'}.
- The observed account/mission is a SOURCE of intelligence, NOT the brand.
- NEVER claim products, services, events, programs, or offers from the observed source as belonging to your brand.
- Transform the insight/opportunity into content relevant to YOUR brand's services and audience.
- NO inventar información de marca que no esté en tu contexto.
- NO buscar en internet ni referenciar datos externos.
- El hook debe ser publicable tal cual (no una descripción de un hook).
- sections[].content debe ser el texto real del guión/slide, no una descripción.
- source_reasoning debe ser un párrafo corto de justificación, no una lista de pasos.

Responde ÚNICAMENTE con un JSON válido con esta estructura exacta (sin markdown ni bloques de código):
{{
    "content_format": "<uno de: {formats}>",
    "objective": "<uno de: {objectives}>",
    "target_audience": "<descripción corta de la audiencia>",
    "angle": "<uno de: {angles}>",
    "core_message": "<mensaje central en 1 oración>",
    "hook": "<primera línea de apertura impactante>",
    "sections": [
        {{"order": 1, "title": "<título opcional o null>", "content": "<texto del bloque>"}},
        {{"order": 2, "title": "<título opcional o null>", "content": "<texto del bloque>"}}
    ],
    "cta": "<llamada a la acción>",
    "visual_direction": "<instrucción conceptual visual>",
    "source_reasoning": "<justificación del porqué de esta pieza>"
}}
"""
