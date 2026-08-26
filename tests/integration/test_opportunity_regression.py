"""
tests/integration/test_opportunity_regression.py

Regresión exacta: Garantiza que no existan referencias obsoletas a `Opportunity.content`.
Persiste una Opportunity con los nuevos campos y ejecuta el retriever + detector
para comprobar que no arroja AttributeError.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.event_envelope import EventEnvelope
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.engines.cognitive.retriever import KnowledgeContextRetriever
from runtime.infrastructure.database.models.knowledge import OpportunityModel
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
from runtime.infrastructure.database.session import async_session
from runtime.workers.signal_worker import SignalWorker
from tests.fakes.fake_llm_provider import FakeLLMProvider


class DummyRedisConsumerGroup:
    def __init__(self):
        self.acked = []

    async def ack(self, entry_id: str) -> None:
        self.acked.append(entry_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_opportunity_stale_reference_regression():
    """
    1. Crea y persiste directamente una Opportunity en base de datos.
    2. Ejecuta todo el flujo del SignalWorker, lo que requiere hacer retrieve del context
       (que incluye la Opportunity) y generar nuevos prompts.
    3. Confirma que no se lanza AttributeError ('Opportunity' object has no attribute 'content').
    """
    mission_id = uuid4()
    
    # 1. Persist the new Opportunity with title, description, priority
    async with async_session() as session:
        model = OpportunityModel(
            id=uuid4(),
            mission_id=mission_id,
            title="Regression Opportunity",
            description="A detailed description of the opportunity",
            priority="medium",
            confidence=0.9,
            created_at=datetime.now(UTC)
        )
        session.add(model)
        await session.commit()
        
    # 2. Setup Worker
    llm = FakeLLMProvider(
        '{"relevant": true, "confidence": 0.9, "evidence": "X", "insight": "Y", "reason": "Z"}'
    )
    # We just need it to not crash. If it calls PatternDetector or OpportunityDetector, it's fine.
    
    engine = CognitiveEngine(llm_provider=llm)
    session_maker = async_sessionmaker(async_session.kw["bind"], expire_on_commit=False)
    consumer = DummyRedisConsumerGroup()
    
    worker = SignalWorker(
        consumer_group=consumer,  # type: ignore
        session_factory=session_maker,
        cognitive_engine=engine,
        llm_provider=llm,
        mission_context="Test context"
    )
    
    # 3. Simulate processing a new signal
    payload = {
        "mission_id": str(mission_id),
        "source": "test",
        "content": "A new signal content to trigger cognitive flow",
        "metadata": {"native_id": "test_opp_regression"},
        "captured_at": datetime.now(UTC).isoformat()
    }
    
    envelope = EventEnvelope(
        event_id=uuid4(),
        event_type="raw_signal_detected",
        payload=payload
    )
    
    # Si hay una referencia a .content, esto lanzará AttributeError dentro de
    # CognitiveEngine (al procesar Relevance) o OpportunityDetector.
    await worker.process_one(entry_id="entry-test", envelope=envelope)
    
    # Confirm commit and XACK
    assert "entry-test" in consumer.acked, "XACK didn't occur, indicating a failure"
