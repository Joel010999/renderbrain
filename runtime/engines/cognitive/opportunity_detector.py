"""
runtime/engines/cognitive/opportunity_detector.py

Detector determinista de oportunidades estratégicas basado en LLM (S5.3).
"""

import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from runtime.contracts.knowledge import Opportunity
from runtime.infrastructure.llm.interfaces import LLMProvider


class OpportunityDetectionResult(BaseModel):
    """Respuesta esperada del LLM para la detección de oportunidades."""
    model_config = ConfigDict(populate_by_name=True)

    opportunity_found: bool
    content: str | None = None
    confidence: float | None = None
    supporting_pattern_indexes: list[int] | None = None
    reason: str | None = None


class OpportunityDetector:
    """
    Detecta posibilidades concretas y accionables derivadas de Patrones.
    """

    def __init__(self, llm_provider: LLMProvider):
        self._llm_provider = llm_provider

    async def detect(
        self,
        mission_id: UUID,
        mission_context: str,
        intelligence_view: "MissionIntelligenceView",
    ) -> tuple[Opportunity | None, list[UUID]]:
        """
        Detecta una oportunidad a partir de los patrones disponibles en la vista de inteligencia.
        Si la cantidad de patrones es 0, retorna (None, []).
        Si encuentra una oportunidad, retorna el objeto Opportunity y la lista de UUIDs de soporte.
        Si falla la validación, levanta ValueError.
        """
        if len(intelligence_view.patterns) == 0:
            return None, []

        prompt = self._build_prompt(mission_context, intelligence_view)
        
        raw_response = await self._llm_provider.complete(prompt)
        
        try:
            parsed = json.loads(raw_response)
            result = OpportunityDetectionResult(**parsed)
        except Exception as e:
            raise ValueError(f"Respuesta inválida del LLM en OpportunityDetector: {e}") from e

        if not result.opportunity_found:
            return None, []

        if not result.content or not result.supporting_pattern_indexes:
            raise ValueError("El LLM indicó opportunity_found=True pero no proveyó content o supporting_pattern_indexes.")

        # Validaciones de índices
        unique_indexes = list(set(result.supporting_pattern_indexes))
        if len(unique_indexes) < 1:
            raise ValueError(f"Se requiere al menos 1 índice de soporte. El LLM devolvió: {unique_indexes}")

        supporting_ids = []
        for idx in unique_indexes:
            if idx < 0 or idx >= len(intelligence_view.patterns):
                raise ValueError(f"Índice de soporte fuera de rango: {idx}. Total patterns: {len(intelligence_view.patterns)}")
            supporting_ids.append(intelligence_view.patterns[idx].id)

        opportunity = Opportunity(
            mission_id=mission_id,
            content=result.content,
            confidence=result.confidence,
        )

        return opportunity, supporting_ids

    def _build_prompt(
        self, mission_context: str, intelligence_view: "MissionIntelligenceView"
    ) -> str:
        """
        Construye el prompt para la detección de oportunidades.
        """
        patterns_text = "\n".join(
            f"[{i}] {summary.content}" for i, summary in enumerate(intelligence_view.patterns)
        )
        
        existing_opps_text = ""
        if intelligence_view.opportunities:
            existing_opps_text = "Oportunidades existentes previamente (NO DUPLICAR CONCEPTUALMENTE):\n" + "\n".join(
                f"- {o.content}" for o in intelligence_view.opportunities
            )
        else:
            existing_opps_text = "No hay oportunidades existentes previas."

        prompt = f"""
Eres RenderBrain, un motor de inteligencia cognitiva.
Tu objetivo es identificar una Oportunidad concreta y accionable basándote estrictamente en los Patrones provistos.
Una oportunidad no es un eslogan, ni un CTA, ni un resumen. Es una acción estratégica sugerida que emerge lógicamente de los patrones detectados.

Contexto de la Misión:
{mission_context}

Patrones Disponibles:
{patterns_text}

{existing_opps_text}

Reglas Críticas:
1. Si no identificas ninguna oportunidad nueva y accionable, o si la oportunidad sugerida es redundante con las existentes, debes retornar "opportunity_found": false.
2. Si identificas una oportunidad, debes respaldarla con al menos 1 índice de los Patrones Disponibles proveídos.
3. Responde estrictamente con un JSON válido usando esta estructura:
{{
    "opportunity_found": true/false,
    "content": "Descripción de la acción estratégica concreta",
    "confidence": 0.88,
    "supporting_pattern_indexes": [0, ...],
    "reason": "Explicación breve"
}}
"""
        return prompt
