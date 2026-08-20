"""
tests/engines/cognitive/test_engine.py

Tests unitarios para CognitiveEngine (S3.3) empleando FakeLLMProvider (Offline).
"""

import json
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.engines.cognitive.engine import CognitiveEngine, CognitiveEngineError
from tests.fakes.fake_llm_provider import FakeLLMProvider


def create_mock_signal(content="Signal content") -> CanonicalSignal:
    from datetime import UTC, datetime
    return CanonicalSignal(
        mission_id=uuid4(),
        source_event_id=uuid4(),
        source="test",
        sensor="test",
        content=content,
        captured_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_relevant_signal_returns_transaction():
    """Valida: Señal relevante retorna KT, trazabilidad de IDs, producer, reason y fake llamado 1 vez."""
    fake_response = json.dumps({
        "relevant": True,
        "evidence": "Fake evidence extracted",
        "insight": "Fake business insight deduced",
        "confidence": 0.85,
        "reason": "Test reason"
    })
    llm = FakeLLMProvider(fake_response)
    llm.complete = MagicMock(side_effect=llm.complete)
    
    engine = CognitiveEngine(llm)
    signal = create_mock_signal()
    tx = await engine.analyze(signal, "Mission context")
    
    assert tx is not None
    # Validar construcción de KT
    assert tx.mission_id == signal.mission_id
    assert tx.producer == "cognitive_engine"
    assert tx.reason == "Test reason"
    
    # Validar trazabilidad
    assert tx.evidence.canonical_signal_id == signal.id
    assert tx.insight.evidence_id == tx.evidence.id
    
    # Evidence e Insight separados correctamente
    assert tx.evidence.content == "Fake evidence extracted"
    assert tx.insight.content == "Fake business insight deduced"
    
    # El fake se llamó exactamente una vez
    llm.complete.assert_called_once()


@pytest.mark.asyncio
async def test_relevant_false_returns_none():
    """Valida: relevant=false aborta silenciosamente devolviendo None."""
    fake_response = json.dumps({
        "relevant": False,
        "evidence": None,
        "insight": None,
        "confidence": None,
        "reason": "Not related to mission"
    })
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)
    
    tx = await engine.analyze(create_mock_signal(), "Mission context")
    assert tx is None


@pytest.mark.asyncio
async def test_invalid_json_raises_error():
    """Valida: JSON inválido levanta excepción controlada."""
    llm = FakeLLMProvider("This is just plain text, not JSON")
    engine = CognitiveEngine(llm)
    
    with pytest.raises(CognitiveEngineError, match="Error al parsear o validar"):
        await engine.analyze(create_mock_signal(), "Mission context")


@pytest.mark.asyncio
async def test_empty_response_raises_error():
    """Valida: Respuesta vacía levanta excepción controlada."""
    llm = FakeLLMProvider("   \n  ")
    engine = CognitiveEngine(llm)
    
    with pytest.raises(CognitiveEngineError, match="respuesta vacía"):
        await engine.analyze(create_mock_signal(), "Mission context")


@pytest.mark.asyncio
async def test_missing_fields_raises_error():
    """Valida: Faltan campos en el JSON (ej: relevant)."""
    fake_response = json.dumps({
        "evidence": "Ev",
        "insight": "In"
    })  # Falta "relevant"
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)
    
    with pytest.raises(CognitiveEngineError, match="Error al parsear o validar"):
        await engine.analyze(create_mock_signal(), "Mission context")


@pytest.mark.asyncio
async def test_relevant_but_missing_evidence_raises_error():
    """Valida: Indica relevant=true pero devuelve null en campos requeridos."""
    fake_response = json.dumps({
        "relevant": True,
        "evidence": None,
        "insight": "Insight without evidence",
        "confidence": 0.8,
        "reason": "Because"
    })
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)
    
    with pytest.raises(CognitiveEngineError, match="faltan campos requeridos"):
        await engine.analyze(create_mock_signal(), "Mission context")


@pytest.mark.asyncio
async def test_empty_mission_context_raises_error():
    """Valida: mission_context vacío falla rápido."""
    llm = FakeLLMProvider()
    engine = CognitiveEngine(llm)
    
    with pytest.raises(CognitiveEngineError, match="mission_context no puede estar vacío"):
        await engine.analyze(create_mock_signal(), "   ")


@pytest.mark.asyncio
async def test_empty_signal_content_raises_error():
    """Valida: signal.content vacío falla rápido."""
    llm = FakeLLMProvider()
    engine = CognitiveEngine(llm)
    signal = create_mock_signal("   ")
    
    with pytest.raises(CognitiveEngineError, match="contenido de la señal no puede estar vacío"):
        await engine.analyze(signal, "Mission context")


@pytest.mark.asyncio
async def test_valid_confidence_is_accepted():
    """Valida: Confidence dentro de [0.0, 1.0] se asigna correctamente."""
    fake_response = json.dumps({
        "relevant": True,
        "evidence": "Ev",
        "insight": "In",
        "confidence": 0.5,
        "reason": "R"
    })
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)
    
    tx = await engine.analyze(create_mock_signal(), "Mission context")
    assert tx is not None
    assert tx.evidence.confidence == 0.5


@pytest.mark.asyncio
async def test_invalid_confidence_above_one_raises_error():
    """Valida: Confidence inválido (>1.0) es rechazado."""
    fake_response = json.dumps({
        "relevant": True,
        "evidence": "Ev",
        "insight": "In",
        "confidence": 1.5,
        "reason": "R"
    })
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)
    
    with pytest.raises(CognitiveEngineError, match="fuera de rango"):
        await engine.analyze(create_mock_signal(), "Mission context")


@pytest.mark.asyncio
async def test_invalid_confidence_below_zero_raises_error():
    """Valida: Confidence inválido (<0.0) es rechazado."""
    fake_response = json.dumps({
        "relevant": True,
        "evidence": "Ev",
        "insight": "In",
        "confidence": -0.1,
        "reason": "R"
    })
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)
    
    with pytest.raises(CognitiveEngineError, match="fuera de rango"):
        await engine.analyze(create_mock_signal(), "Mission context")


@pytest.mark.asyncio
async def test_json_wrapped_in_markdown_is_parsed():
    """Valida: Limpieza básica de tags markdown de código JSON."""
    fake_response = "```json\n" + json.dumps({
        "relevant": True,
        "evidence": "Ev clean",
        "insight": "In",
        "confidence": 0.8,
        "reason": "R"
    }) + "\n```"
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)
    
    tx = await engine.analyze(create_mock_signal(), "Mission context")
    assert tx is not None
    assert tx.evidence.content == "Ev clean"
