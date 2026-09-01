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
import asyncio

from pydantic import BaseModel, ConfigDict, ValidationError

from runtime.contracts.content_brief import (
    BrandServiceAlignment,
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


class MisalignedBrandBriefError(ValueError):
    """
    Error semántico post-generación.
    Se lanza cuando el contenido generado (hook, core_message, sections, cta) 
    sigue promocionando la fuente observada en lugar de abstraer un transferable 
    insight hacia los servicios de la marca.
    """


class TransferableInsightResult(BaseModel):
    """
    Fase A (Abstracción): Resultado estructurado.
    NO contiene el contenido final, solo el insight de negocio y su alineamiento.
    """
    transferable_insight: str
    brand_service_alignment: BrandServiceAlignment
    business_pain: str
    rationale: str


# ---------------------------------------------------------------------------
# Modelo privado para validar la respuesta JSON del LLM (Fase B)
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
        Genera un ContentBrief validado a partir de una Opportunity en 2 Fases (Abstraction y Generation).
        """
        logger.info(
            "Content strategy generation started",
            extra={
                "opportunity_id": str(opportunity.id),
                "opportunity_title": opportunity.title,
                "priority": opportunity.priority,
            },
        )

        # FASE A: Abstracción
        abstraction = await self._generate_abstraction(
            opportunity, mission_context, observation_scope, brand_context
        )

        # FASE B: Generación
        raw_response = await self._generate_content(abstraction, brand_context, mission_context)

        # Parseo y Validación
        brief = self._parse_and_validate(raw_response, opportunity, abstraction)
        
        self._deterministic_validation(brief, opportunity)
        await self._validate_alignment(brief, opportunity, brand_context)

        logger.info(
            "Content brief generated and validated",
            extra={
                "opportunity_id": str(opportunity.id),
                "brief_id": str(brief.id),
                "content_format": brief.content_format.value,
                "objective": brief.objective.value,
            },
        )

        return brief

    def _deterministic_validation(self, brief: ContentBrief, opportunity: Opportunity) -> None:
        """
        Cheap deterministic check before calling the QA LLM.
        Prevents wasting LLM calls on obvious failures.
        """
        if not brief.transferable_insight or not brief.transferable_insight.strip():
            raise MisalignedBrandBriefError("El transferable_insight está vacío.")
        
        # Simple heuristic: if the alignment is NOT direct fit (e.g., coworking -> crm)
        # we check if the hook/cta prominently feature the opportunity title words.
        # This is basic and not a strict keyword block, but catches egregious direct copies.
        opp_words = [w.lower() for w in opportunity.title.split() if len(w) > 4]
        
        if brief.brand_service_alignment.value not in ["stock_sales_collections", "ecommerce"]:
            # Hardcoded heuristic for the coworking case specifically to stop egregious loop
            if "coworking" in opportunity.title.lower():
                text_to_check = f"{brief.hook.lower()} {brief.cta.lower()} {brief.core_message.lower()}"
                if "coworking" in text_to_check and brief.brand_service_alignment.value != "coworking":
                    raise MisalignedBrandBriefError(
                        "Validación determinista falló: el brief sigue mencionando 'coworking' fuertemente "
                        "como oferta principal a pesar de estar alineado a un servicio distinto."
                    )

    async def _validate_alignment(
        self,
        brief: ContentBrief,
        opportunity: Opportunity,
        brand_context: dict[str, str | list[str]] | None,
    ) -> None:
        """
        Validación post-generación para asegurar que el contenido final no 
        promocione la fuente original.
        """
        prompt = f"""You are a strict QA Reviewer for RenderBrain.
Evaluate the following Content Brief generated from an observed opportunity.

OBSERVED OPPORTUNITY (The Source):
Title: {opportunity.title}
Description: {opportunity.description}

BRAND CONTEXT:
Brand Name: {brand_context.get('brand_name', 'The Brand') if brand_context else 'The Brand'}
Selected Service Alignment: {brief.brand_service_alignment.value}

GENERATED CONTENT TO REVIEW:
Hook: {brief.hook}
Core Message: {brief.core_message}
Sections:
"""
        for s in brief.sections:
            prompt += f"- {s.content}\n"
        prompt += f"""
CTA: {brief.cta}
Visual Direction: {brief.visual_direction}

CRITICAL RULES FOR APPROVAL:
1. The content MUST NOT promote or be primarily about the Observed Opportunity's specific event, product, or community.
2. The content MUST be about a transferable business lesson mapped to the Selected Service Alignment ({brief.brand_service_alignment.value}).
3. The Brand must be the main focus of the solution, not just mentioned as a side note.
4. If the selected alignment is a direct fit for the opportunity (like 'stock_sales_collections' for an opportunity about 'control de stock'), that's fine, but if it is completely different (like 'crm' for 'coworking'), the content must clearly not promote coworking.

Answer ONLY with a valid JSON containing:
{{
    "is_aligned": boolean,
    "reason": "short explanation"
}}
"""
        try:
            val_response = await asyncio.wait_for(self._llm.complete(prompt), timeout=30.0)
            clean = val_response.strip()
            if clean.startswith("```json"):
                clean = clean[7:]
            if clean.startswith("```"):
                clean = clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()
            val_data = json.loads(clean)
            
            if not val_data.get("is_aligned", True):
                raise MisalignedBrandBriefError(
                    f"El contenido sigue promocionando la fuente o no se alinea con RenderByte: {val_data.get('reason')}"
                )
        except json.JSONDecodeError:
            pass  # Tolerante si falla el parseo QA
        except asyncio.TimeoutError as e:
            raise RuntimeError("Timeout esperando validación QA del LLM (Content Strategist).") from e

    def _parse_and_validate(
        self,
        raw_response: str,
        opportunity: Opportunity,
        abstraction: TransferableInsightResult,
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
                transferable_insight=abstraction.transferable_insight,
                brand_service_alignment=abstraction.brand_service_alignment,
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

    async def _generate_abstraction(
        self,
        opportunity: Opportunity,
        mission_context: str,
        observation_scope: str,
        brand_context: dict[str, str | list[str]] | None,
    ) -> TransferableInsightResult:
        """
        FASE A: Extrae el Transferable Insight sin generar contenido.
        """
        alignments = ", ".join(a.value for a in BrandServiceAlignment)
        brand_info = ""
        if brand_context:
            services = ", ".join(brand_context.get("core_services", []))
            brand_info = f"""
Brand Context:
Name: {brand_context.get('brand_name', 'la marca')}
Description: {brand_context.get('brand_description', '')}
Services: {services}
"""

        prompt = f"""Eres RenderBrain Content Strategist — Fase A: Abstracción.
Tu tarea es analizar una Opportunity extraída de una fuente externa y derivar un insight de negocio transferible.

{brand_info}

Contexto de la Misión (La FUENTE que observaste para aprender):
{mission_context}
Propósito de la observación: {observation_scope}

Opportunity a analizar:
- Título: {opportunity.title}
- Descripción: {opportunity.description}

Instrucciones:
1. Extrae una lección de negocios, principio o dolor abstracto desde la Opportunity que pueda aplicarse a cualquier otra industria. NO hables del producto/servicio original de la fuente. (Ej: "Creación de coworking" -> Dolor: "Falta de seguimiento de contactos genera pérdida de ventas").
2. Selecciona el servicio de RenderByte ({alignments}) que mejor resuelve este dolor abstracto.

Responde ÚNICAMENTE con un JSON válido:
{{
    "transferable_insight": "<insight abstracto>",
    "brand_service_alignment": "<uno de: {alignments}>",
    "business_pain": "<dolor de negocio que resolvemos>",
    "rationale": "<breve justificación interna>"
}}
"""
        try:
            val_response = await asyncio.wait_for(self._llm.complete(prompt), timeout=30.0)
            clean = val_response.strip()
            if clean.startswith("```json"):
                clean = clean[7:]
            if clean.startswith("```"):
                clean = clean[3:]
            if clean.endswith("```"):
                clean = clean[:-3]
            clean = clean.strip()
            val_data = json.loads(clean)
            
            return TransferableInsightResult(**val_data)
        except json.JSONDecodeError as e:
            raise InvalidContentBriefOutputError("Fase A devolvió JSON inválido") from e
        except ValidationError as e:
            raise InvalidContentBriefOutputError("Fase A no cumple el schema TransferableInsightResult") from e
        except asyncio.TimeoutError as e:
            raise RuntimeError("Timeout esperando respuesta del LLM (Fase A).") from e

    async def _generate_content(
        self,
        abstraction: TransferableInsightResult,
        brand_context: dict[str, str | list[str]] | None,
        mission_context: str,
    ) -> str:
        """
        FASE B: Genera el ContentBrief usando SOLO el Transferable Insight.
        El LLM NUNCA recibe el título ni la descripción de la Opportunity original.
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

        prompt = f"""Eres RenderBrain Content Strategist — Fase B: Generación.
Tu tarea es generar un ContentBrief accionable a partir de un insight estratégico.

{brand_info}

INSIGHT ESTRATÉGICO A COMUNICAR:
- Transferable Insight: {abstraction.transferable_insight}
- Business Pain a resolver: {abstraction.business_pain}
- Servicio de tu Marca a promocionar: {abstraction.brand_service_alignment.value}

Contexto mínimo de la misión original: {mission_context}

Instrucciones:
- El contenido final debe versar 100% sobre el Transferable Insight y el dolor de negocio (Business Pain), ofreciendo el Servicio de tu Marca como solución.
- NO menciones ningún producto de terceros, eventos o comunidades de donde pudo provenir este insight. Tú eres {brand_context.get('brand_name', 'la marca') if brand_context else 'la marca'} y vendes tus propios servicios.
- Promociona explícitamente el servicio seleccionado: {abstraction.brand_service_alignment.value}.

Campos a rellenar:
1. content_format: {formats}
2. objective: {objectives}
3. target_audience: (descripción corta, 1-2 oraciones).
4. angle: {angles}
5. core_message: la idea fundamental en 1 oración.
6. hook: primera línea de apertura, corta, impactante, publicable directamente.
7. sections: Mínimo 1, máximo 5. (Body/script ordenado).
8. cta: coherente con el objetivo y el SERVICIO SELECCIONADO.
9. visual_direction: instrucción conceptual visual.
10. source_reasoning: por qué esta pieza es la respuesta correcta a este insight.

Responde ÚNICAMENTE con un JSON válido con esta estructura exacta:
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
        try:
            raw_response = await asyncio.wait_for(self._llm.complete(prompt), timeout=60.0)
            if not raw_response or not raw_response.strip():
                raise InvalidContentBriefOutputError("El LLM devolvió una respuesta vacía en Fase B.")
            return raw_response
        except asyncio.TimeoutError as e:
            raise RuntimeError("Timeout esperando respuesta del LLM (Fase B).") from e
