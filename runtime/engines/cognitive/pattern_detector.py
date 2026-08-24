"""
runtime/engines/cognitive/pattern_detector.py

Detector determinista de patrones basado en LLM (S5.2).
"""

import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from runtime.contracts.knowledge import Pattern
from runtime.infrastructure.llm.interfaces import LLMProvider


class PatternDetectionResult(BaseModel):
    """Respuesta esperada del LLM para la detección de patrones."""
    model_config = ConfigDict(populate_by_name=True)

    pattern_found: bool
    content: str | None = None
    confidence: float | None = None
    supporting_insight_indexes: list[int] | None = None
    reason: str | None = None


class PatternDetector:
    """
    Detecta recurrencias significativas a través de múltiples Insights.
    """

    def __init__(self, llm_provider: LLMProvider, min_insights_threshold: int = 3):
        self._llm_provider = llm_provider
        self._min_insights_threshold = min_insights_threshold

    async def detect(
        self,
        mission_id: UUID,
        mission_context: str,
        intelligence_view: "MissionIntelligenceView",
    ) -> tuple[Pattern | None, list[UUID]]:
        """
        Detecta un patrón a partir de los insights recientes en la vista de inteligencia.
        Si la cantidad de insights es menor al umbral, retorna (None, []).
        Si encuentra un patrón, retorna el objeto Pattern y la lista de UUIDs de soporte.
        Si falla la validación, levanta ValueError.
        """
        if len(intelligence_view.insights) < self._min_insights_threshold:
            return None, []

        prompt = self._build_prompt(mission_context, intelligence_view)
        
        raw_response = await self._llm_provider.complete(prompt)
        
        try:
            parsed = json.loads(raw_response)
            result = PatternDetectionResult(**parsed)
        except Exception as e:
            raise ValueError(f"Respuesta inválida del LLM en PatternDetector: {e}") from e

        if not result.pattern_found:
            return None, []

        if not result.content or not result.supporting_insight_indexes:
            raise ValueError("El LLM indicó pattern_found=True pero no proveyó content o supporting_insight_indexes.")

        # Validaciones de índices
        unique_indexes = list(set(result.supporting_insight_indexes))
        if len(unique_indexes) < 2:
            raise ValueError(f"Se requieren al menos 2 índices de soporte. El LLM devolvió: {unique_indexes}")

        supporting_ids = []
        for idx in unique_indexes:
            if idx < 0 or idx >= len(intelligence_view.insights):
                raise ValueError(f"Índice de soporte fuera de rango: {idx}. Total insights: {len(intelligence_view.insights)}")
            supporting_ids.append(intelligence_view.insights[idx].id)

        pattern = Pattern(
            mission_id=mission_id,
            content=result.content,
            confidence=result.confidence,
            support_count=len(supporting_ids),
        )

        return pattern, supporting_ids

    def _build_prompt(
        self, mission_context: str, intelligence_view: "MissionIntelligenceView"
    ) -> str:
        """
        Construye el prompt para la detección de patrones.
        """
        insights_text = "\n".join(
            f"[{i}] {summary.content}" for i, summary in enumerate(intelligence_view.insights)
        )
        
        existing_patterns_text = ""
        if intelligence_view.patterns:
            existing_patterns_text = "Patrones existentes previamente (NO DUPLICAR):\n" + "\n".join(
                f"- {p.content}" for p in intelligence_view.patterns
            )
        else:
            existing_patterns_text = "No hay patrones existentes previos."

        prompt = f"""
Eres RenderBrain, un motor de inteligencia cognitiva analizando un flujo de eventos continuos.
Tu objetivo es identificar patrones o recurrencias significativas basándote estrictamente en los Insights provistos.

Contexto de la Misión:
{mission_context}

Insights Recientes (índices zero-based):
{insights_text}

{existing_patterns_text}

Reglas Críticas:
1. Un patrón es una recurrencia, un comportamiento repetido, o una tendencia observable. No es un simple resumen.
2. Si no identificas ningún patrón nuevo y valioso, debes retornar "pattern_found": false.
3. Si identificas un patrón, debes respaldarlo con al menos 2 índices de los Insights Recientes proveídos.
4. supporting_insight_indexes MUST use exactly the zero-based indexes shown above (e.g., 0, 1, 2...). Do NOT use 1-based numbering.
5. Responde estrictamente con un JSON válido usando esta estructura:
{{
    "pattern_found": true/false,
    "content": "Descripción detallada del patrón encontrado (si aplica)",
    "confidence": 0.90,
    "supporting_insight_indexes": [0, 2, ...],
    "reason": "Explicación breve"
}}
"""
        return prompt
