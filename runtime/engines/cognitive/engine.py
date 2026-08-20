"""
runtime/engines/cognitive/engine.py

Implementación del Cognitive Engine (S3.3).
Recibe una CanonicalSignal y un LLMProvider, y produce una KnowledgeTransaction.
"""

import json

from pydantic import BaseModel, ValidationError

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.knowledge import Evidence, Insight, KnowledgeTransaction, MissionIntelligenceView
from runtime.infrastructure.llm.interfaces import LLMProvider


class _LLMParsedResult(BaseModel):
    """Modelo privado para validar estrictamente la respuesta JSON del LLM."""
    relevant: bool
    evidence: str | None = None
    insight: str | None = None
    confidence: float | None = None
    reason: str | None = None


class CognitiveEngineError(Exception):
    """Error interno controlado del Cognitive Engine."""
    pass


class CognitiveEngine:
    """
    Componente inteligente de RenderBrain.
    Mapea de CanonicalSignal a KnowledgeTransaction usando un LLMProvider inyectado.
    """

    def __init__(self, llm: LLMProvider) -> None:
        self._llm = llm

    async def analyze(
        self, signal: CanonicalSignal, mission_context: str, knowledge_context: MissionIntelligenceView | None = None
    ) -> KnowledgeTransaction | None:
        """
        Analiza una señal bajo un contexto de misión y decide si extraer conocimiento.
        Retorna KnowledgeTransaction si es relevante, o None en caso contrario.
        """
        if not mission_context or not mission_context.strip():
            raise CognitiveEngineError("El mission_context no puede estar vacío.")
        if not signal.content or not signal.content.strip():
            raise CognitiveEngineError("El contenido de la señal no puede estar vacío.")

        previous_insights_text = "Sin conocimiento previo."
        if knowledge_context and knowledge_context.insights:
            lines = [f"- {i.content} (Confianza: {i.confidence})" for i in knowledge_context.insights]
            previous_insights_text = "\n".join(lines)
            
        previous_patterns_text = "Sin patrones detectados."
        if knowledge_context and knowledge_context.patterns:
            lines = [f"- {p.content} (Soporte: {p.support_count}, Confianza: {p.confidence})" for p in knowledge_context.patterns]
            previous_patterns_text = "\n".join(lines)
            
        previous_opportunities_text = "Sin oportunidades detectadas."
        if knowledge_context and knowledge_context.opportunities:
            lines = [f"- {o.content} (Confianza: {o.confidence})" for o in knowledge_context.opportunities]
            previous_opportunities_text = "\n".join(lines)

        prompt = f"""
Evalúa la siguiente señal bajo este contexto de misión y conocimiento previo acumulado.

Contexto de la misión:
{mission_context}

Conocimiento previo acumulado (Previous Insights):
{previous_insights_text}

Patrones existentes (Existing Patterns):
{previous_patterns_text}

Oportunidades existentes (Existing Opportunities):
{previous_opportunities_text}


Señal original (Current signal):
{signal.content}

Instrucciones:
1. ¿Es la señal actual relevante para la misión? (relevant: true/false)
2. Si es relevante, extrae:
   - evidence: Un hecho observable directamente soportado SÓLO por la señal actual (sin inventar).
   - insight: Una deducción o interpretación de negocio basada puramente en esa evidencia ACTUAL, combinada opcionalmente con el conocimiento previo acumulado.
   - confidence: Nivel de confianza entre 0.0 y 1.0.
   - reason: Por qué es relevante o por qué no lo es.

Reglas Críticas de Razonamiento:
- NO asumas que el conocimiento previo es la verdad absoluta.
- Distingue claramente entre la evidencia observable en la señal ACTUAL y el nuevo Insight derivado usando la señal actual + el contexto acumulado.
- NO debes repetir un Insight viejo como si fuera nuevo. Si la señal actual no aporta nada nuevo sobre el conocimiento previo, marcala como irrelevante o extrae un insight cualitativamente distinto.

Responde ÚNICAMENTE con un JSON válido usando esta estructura exacta (sin markdown ni bloques de código extra):
{{
    "relevant": true o false,
    "evidence": "texto o null",
    "insight": "texto o null",
    "confidence": 0.85,
    "reason": "texto o null"
}}
"""
        response_str = await self._llm.complete(prompt)
        
        if not response_str or not response_str.strip():
            raise CognitiveEngineError("El LLM devolvió una respuesta vacía.")

        try:
            # Limpieza básica por si el LLM incluye markdown tags
            clean_str = response_str.strip()
            if clean_str.startswith("```json"):
                clean_str = clean_str[7:]
            if clean_str.endswith("```"):
                clean_str = clean_str[:-3]
            clean_str = clean_str.strip()
            
            parsed_data = json.loads(clean_str)
            parsed_result = _LLMParsedResult.model_validate(parsed_data)
        except (json.JSONDecodeError, ValidationError) as e:
            raise CognitiveEngineError(f"Error al parsear o validar la respuesta del LLM: {e}")

        # Si el LLM dice que no es relevante, abortar mapeo y devolver None (sin inventar datos)
        if not parsed_result.relevant:
            return None

        # Si es relevante, exigir los campos obligatorios
        if not parsed_result.evidence or not parsed_result.insight:
            raise CognitiveEngineError(
                "La respuesta indicó relevancia pero faltan campos requeridos (evidence o insight)."
            )

        # Validar rangos de confidence
        if parsed_result.confidence is not None and (
            parsed_result.confidence < 0.0 or parsed_result.confidence > 1.0
        ):
            raise CognitiveEngineError(
                "El confidence devuelto por el LLM está fuera de rango (0.0 a 1.0)."
            )

        # 1. Construir Evidence mapeando las variables
        evidence = Evidence(
            mission_id=signal.mission_id,
            canonical_signal_id=signal.id,
            content=parsed_result.evidence,
            confidence=parsed_result.confidence,
        )

        # 2. Construir Insight mapeando las variables
        insight = Insight(
            mission_id=signal.mission_id,
            evidence_id=evidence.id,  # Referencia inmutable a la Evidence recién creada
            content=parsed_result.insight,
            confidence=parsed_result.confidence,
        )

        # 3. Empaquetar todo en la KnowledgeTransaction atómica
        transaction = KnowledgeTransaction(
            mission_id=signal.mission_id,
            action="CREATE_KNOWLEDGE",
            evidence=evidence,
            insight=insight,
            producer="cognitive_engine",
            reason=parsed_result.reason,
        )

        return transaction
