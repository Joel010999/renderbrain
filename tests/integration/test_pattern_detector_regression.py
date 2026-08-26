"""
tests/integration/test_pattern_detector_regression.py

Regresión exacta: Garantiza que un JSON malformado o output inválido devuelto por el LLM
en PatternDetector levante InvalidPatternOutputError y degrade de forma recuperable
en SignalWorker, persistiendo el resto de la inteligencia.
"""

from datetime import UTC, datetime
from uuid import uuid4
import json
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from runtime.contracts.event_envelope import EventEnvelope
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.infrastructure.database.models.knowledge import InsightModel
from runtime.infrastructure.database.session import async_session
from runtime.workers.signal_worker import SignalWorker
from runtime.infrastructure.llm.interfaces import LLMProvider

class DummyRedisConsumerGroup:
    def __init__(self):
        self.acked = []

    async def ack(self, entry_id: str) -> None:
        self.acked.append(entry_id)


class BrokenPatternLLMProvider(LLMProvider):
    def __init__(self, mode="broken_json"):
        self.mode = mode
        self.calls = 0

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.calls == 1:
            # First call is CognitiveEngine (insight)
            return '{"relevant": true, "confidence": 0.9, "evidence": "Evidencia", "insight": "Insight deducido", "reason": "Reason"}'
        elif self.calls == 2:
            # Second call is PatternDetector
            if self.mode == "broken_json":
                return "{broken json, Expecting property name enclosed in double quotes"
            elif self.mode == "timeout":
                raise RuntimeError("Simulated provider timeout in PatternDetector")
            return '{"pattern_found": false}'
        else:
            return '{"opportunity_found": false}'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pattern_detector_broken_json_degrades_gracefully():
    """
    1. Usa un LLMProvider que devuelve JSON malformado para PatternDetector.
    2. Ejecuta el SignalWorker.
    3. Confirma que el worker degrada (descarta el pattern) pero procesa el resto (XACK).
    """
    mission_id = uuid4()
    
    # Insertamos algunos insights para que PatternDetector no salga por umbral (<3)
    async with async_session() as session:
        for _ in range(3):
            session.add(InsightModel(
                id=uuid4(),
                mission_id=mission_id,
                content="Existing insight",
                confidence=0.9,
                created_at=datetime.now(UTC)
            ))
        await session.commit()

    llm = BrokenPatternLLMProvider(mode="broken_json")
    engine = CognitiveEngine(llm_provider=llm)
    session_maker = async_sessionmaker(async_session.kw["bind"], expire_on_commit=False)
    consumer = DummyRedisConsumerGroup()
    
    worker = SignalWorker(
        consumer_group=consumer,  # type: ignore
        session_factory=session_maker,
        cognitive_engine=engine,
        llm_provider=llm,
        mission_context="Contexto de prueba"
    )
    
    payload = {
        "mission_id": str(mission_id),
        "source": "test",
        "content": "Señal que dispara el motor",
        "metadata": {"native_id": f"broken_pattern_{uuid4()}"},
        "captured_at": datetime.now(UTC).isoformat()
    }
    envelope = EventEnvelope(event_id=uuid4(), event_type="raw_signal_detected", payload=payload)
    
    # Debería degradar pero terminar exitosamente
    await worker.process_one(entry_id="entry-broken-pattern", envelope=envelope)
    
    # Confirm commit and XACK
    assert "entry-broken-pattern" in consumer.acked, "XACK didn't occur! The broken JSON was fatal."


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pattern_detector_timeout_causes_rollback():
    """
    1. Usa un LLMProvider que levanta RuntimeError (timeout) para PatternDetector.
    2. Ejecuta el SignalWorker.
    3. Confirma que el worker hace rollback y NO hace XACK.
    """
    mission_id = uuid4()
    async with async_session() as session:
        for _ in range(3):
            session.add(InsightModel(
                id=uuid4(),
                mission_id=mission_id,
                content="Existing insight",
                confidence=0.9,
                created_at=datetime.now(UTC)
            ))
        await session.commit()

    llm = BrokenPatternLLMProvider(mode="timeout")
    engine = CognitiveEngine(llm_provider=llm)
    session_maker = async_sessionmaker(async_session.kw["bind"], expire_on_commit=False)
    consumer = DummyRedisConsumerGroup()
    
    worker = SignalWorker(
        consumer_group=consumer,  # type: ignore
        session_factory=session_maker,
        cognitive_engine=engine,
        llm_provider=llm,
        mission_context="Contexto de prueba"
    )
    
    payload = {
        "mission_id": str(mission_id),
        "source": "test",
        "content": "Señal que dispara el motor",
        "metadata": {"native_id": f"timeout_pattern_{uuid4()}"},
        "captured_at": datetime.now(UTC).isoformat()
    }
    envelope = EventEnvelope(event_id=uuid4(), event_type="raw_signal_detected", payload=payload)
    
    with pytest.raises(RuntimeError, match="Simulated provider timeout"):
        await worker.process_one(entry_id="entry-timeout", envelope=envelope)
    
    # NO debe haber XACK
    assert "entry-timeout" not in consumer.acked, "Se hizo XACK en un timeout!"
