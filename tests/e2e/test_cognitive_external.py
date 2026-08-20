"""
tests/e2e/test_cognitive_external.py

Test E2E contra OpenAI (S3.3) usando el CognitiveEngine real. Opt-in explícito.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.infrastructure.llm.openai import OpenAIAdapter
from runtime.shared.config import settings


@pytest.mark.external
@pytest.mark.asyncio
async def test_cognitive_engine_real_call():
    """
    Test E2E contra la API de OpenAI usando CognitiveEngine.
    Consume créditos, ejecutar solo bajo demanda con --run-external.
    """
    if not settings.OPENAI_API_KEY:
        pytest.skip("OPENAI_API_KEY no configurada — test externo omitido.")

    # 1. Instanciar infraestructura (Adapter)
    adapter = OpenAIAdapter()
    
    # 2. Inyectar al motor cognitivo
    engine = CognitiveEngine(llm=adapter)

    # 3. Preparar un CanonicalSignal ficticio pero realista
    signal = CanonicalSignal(
        mission_id=uuid4(),
        source_event_id=uuid4(),
        source="test_excel",
        sensor="test_manual",
        content="Las ventas del producto X cayeron un 20% en el último trimestre debido a retrasos en la distribución en Europa.",
        captured_at=datetime.now(UTC)
    )
    
    mission_context = "Nuestro objetivo es identificar causas de caídas de ventas en Europa."

    # 4. Analizar (1 sola llamada a OpenAI)
    transaction = await engine.analyze(signal, mission_context)

    # 5. Comprobar que devolvió algo válido
    assert transaction is not None
    assert transaction.mission_id == signal.mission_id
    assert transaction.producer == "cognitive_engine"
    
    # Genealogía
    assert transaction.evidence.canonical_signal_id == signal.id
    assert transaction.insight.evidence_id == transaction.evidence.id

    # Validar que extrajo texto (no nulo ni vacío)
    assert isinstance(transaction.evidence.content, str)
    assert len(transaction.evidence.content) > 0
    assert isinstance(transaction.insight.content, str)
    assert len(transaction.insight.content) > 0
    
    # Confidence
    if transaction.evidence.confidence is not None:
        assert 0.0 <= transaction.evidence.confidence <= 1.0
