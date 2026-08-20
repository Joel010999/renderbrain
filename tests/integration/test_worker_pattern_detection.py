"""
tests/integration/test_worker_pattern_detection.py
"""
import json
import pytest
from uuid import uuid4
from datetime import datetime, UTC

from sqlalchemy import select

from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.events import RedisConsumerGroup, RedisEventBus, wrap_and_publish
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.models.knowledge import (
    EvidenceModel,
    InsightModel,
    KnowledgeTransactionModel,
    PatternModel,
)
from runtime.infrastructure.database.models.mission import MissionModel, ProcessedSignalModel
from runtime.infrastructure.redis.client import get_redis_client
from runtime.workers.signal_worker import SignalWorker
from tests.infrastructure.llm.test_llm_adapters import FakeLLMProvider

_TEST_STREAM = "renderbrain:test:events:pattern"
_TEST_GROUP = "test-pattern-workers"

async def _create_mission(session, mission_id: str):
    mission = MissionModel(
        id=mission_id,
        name="Test Pattern Mission",
        enabled=True,
        interval_seconds=300,
        target="test_target",
        source="instagram",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC)
    )
    session.add(mission)
    await session.commit()
    return mission

@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_cumulative_pattern_and_crash_recovery():
    mission_id = str(uuid4())
    async with async_session() as session:
        mission = await _create_mission(session, mission_id)

    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    bus = RedisEventBus(redis, _TEST_STREAM)
    cg = RedisConsumerGroup(redis, _TEST_STREAM, _TEST_GROUP, "worker-1")
    await cg.ensure_group()

    # Usamos un FakeLLMProvider dinámico
    class DynamicFakeLLM(FakeLLMProvider):
        def __init__(self):
            super().__init__("{}")
            self.call_count = 0
            self.fail_on_call = -1

        async def complete(self, prompt: str) -> str:
            self.call_count += 1
            if self.call_count == self.fail_on_call:
                raise Exception("Forced LLM Error in Pattern Detection")
            
            # Si estamos en CognitiveEngine (prompt corto) o PatternDetector (prompt con 'Reglas Críticas')
            if "Un patrón es una recurrencia" in prompt or "pattern_found" in prompt:
                # Retornamos pattern_found=True
                return json.dumps({
                    "pattern_found": True,
                    "content": "Cumulative Pattern Found",
                    "confidence": 0.9,
                    "supporting_insight_indexes": [0, 1],
                    "reason": "Because they match"
                })
            elif "Una oportunidad de negocio" in prompt or "opportunity_found" in prompt:
                return json.dumps({
                    "opportunity_found": True,
                    "content": "Cumulative Opportunity Found",
                    "confidence": 0.9,
                    "supporting_pattern_indexes": [0],
                    "reason": "Because they match"
                })
            else:
                return json.dumps({
                    "relevant": True,
                    "evidence": "Evidencia",
                    "insight": "Insight",
                    "confidence": 0.8,
                    "reason": "Relevant"
                })

    llm = DynamicFakeLLM()
    engine = CognitiveEngine(llm)
    worker = SignalWorker(cg, session_factory=async_session, mission_context="Context", cognitive_engine=engine, llm_provider=llm)

    # Procesar 3 señales para superar el umbral
    for i in range(1, 4):
        signal = RawSignalDetected(
            sensor="fake",
            source="instagram",
            mission_id=mission_id,
            raw_payload={"data": {"id": f"sig_{i}", "caption": "Some valid content text"}}
        )
        env = await wrap_and_publish(signal, bus)
        
        messages = await cg.read_new(count=1)
        entry_id, read_env = messages[0]
        
        # En la 3ra señal forzamos fallo si queremos probar recovery, pero aquí testearemos éxito primero
        await worker.process_one(entry_id, read_env)

    # Validaciones Finales
    async with async_session() as session:
        # Debe haber 3 signals, 3 insights, 3 processed signals
        count_cs = await session.execute(select(CanonicalSignalModel).where(CanonicalSignalModel.mission_id == mission_id))
        assert len(count_cs.scalars().all()) == 3
        
        count_ps = await session.execute(select(ProcessedSignalModel).where(ProcessedSignalModel.mission_id == mission_id))
        assert len(count_ps.scalars().all()) == 3
        
        count_in = await session.execute(select(InsightModel).where(InsightModel.mission_id == mission_id))
        assert len(count_in.scalars().all()) == 3
        
        # Y debe haber 1 Pattern
        count_pat = await session.execute(select(PatternModel).where(PatternModel.mission_id == mission_id))
        patterns = count_pat.scalars().all()
        try:
            assert len(patterns) == 1, f"Expected 1 pattern, got {len(patterns)}. Patterns: {patterns}"
            assert patterns[0].content == "Cumulative Pattern Found"
            assert patterns[0].support_count == 2
        except Exception as e:
            print(f"ASSERTION ERROR: {e}")
            raise

    # Verificamos mensajes XACKed
    pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
    assert len(pending) == 0

@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_crash_recovery_on_pattern_failure():
    mission_id = str(uuid4())
    async with async_session() as session:
        mission = await _create_mission(session, mission_id)

    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    bus = RedisEventBus(redis, _TEST_STREAM)
    cg = RedisConsumerGroup(redis, _TEST_STREAM, _TEST_GROUP, "worker-1")
    await cg.ensure_group()

    # Pre-cargar 2 insights exitosos
    class OkLLM(FakeLLMProvider):
        async def complete(self, prompt: str) -> str:
            return json.dumps({
                "relevant": True,
                "evidence": "Evidencia",
                "insight": "Insight",
                "confidence": 0.8,
                "reason": "Relevant"
            })
    
    llm_ok = OkLLM()
    worker_ok = SignalWorker(cg, session_factory=async_session, mission_context="Context", cognitive_engine=CognitiveEngine(llm_ok), llm_provider=llm_ok)
    for i in range(1, 3):
        signal = RawSignalDetected(
            sensor="fake",
            source="instagram",
            mission_id=mission_id,
            raw_payload={"data": {"id": f"crash_{i}", "caption": "Some valid content text"}}
        )
        env = await wrap_and_publish(signal, bus)
        messages = await cg.read_new(count=1)
        await worker_ok.process_one(messages[0][0], messages[0][1])

    # Ahora procesamos la 3ra, forzando un error en el Pattern Detector
    class FailLLM(FakeLLMProvider):
        async def complete(self, prompt: str) -> str:
            if "pattern_found" in prompt:
                raise Exception("Forced LLM Error in Pattern Detection")
            return json.dumps({
                "relevant": True,
                "evidence": "Evidencia",
                "insight": "Insight",
                "confidence": 0.8,
                "reason": "Relevant"
            })

    llm_fail = FailLLM()
    worker_fail = SignalWorker(cg, session_factory=async_session, mission_context="Context", cognitive_engine=CognitiveEngine(llm_fail), llm_provider=llm_fail)
    
    signal_3 = RawSignalDetected(
        sensor="fake",
        source="instagram",
        mission_id=mission_id,
        raw_payload={"data": {"id": "crash_3", "caption": "Some valid content text"}}
    )
    env_3 = await wrap_and_publish(signal_3, bus)
    messages = await cg.read_new(count=1)
    entry_id_3, read_env_3 = messages[0]
    
    # Esto fallará y propagará la excepción, pero dejaremos el mensaje pending
    with pytest.raises(Exception, match="Forced LLM Error in Pattern Detection"):
        await worker_fail.process_one(entry_id_3, read_env_3)
    
    # Validar que NO se guardaron los artefactos de la 3ra señal
    async with async_session() as session:
        # Seguimos teniendo solo 2
        count_cs = await session.execute(select(CanonicalSignalModel).where(CanonicalSignalModel.mission_id == mission_id))
        assert len(count_cs.scalars().all()) == 2
        
        count_ps = await session.execute(select(ProcessedSignalModel).where(ProcessedSignalModel.mission_id == mission_id))
        assert len(count_ps.scalars().all()) == 2
        
        count_in = await session.execute(select(InsightModel).where(InsightModel.mission_id == mission_id))
        assert len(count_in.scalars().all()) == 2
        
        # 0 patterns
        count_pat = await session.execute(select(PatternModel).where(PatternModel.mission_id == mission_id))
        assert len(count_pat.scalars().all()) == 0

    # Validar que el mensaje sigue pending en Redis
    pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
    # Debe haber 1 mensaje pendiente, que es el entry_id_3
    assert len(pending) > 0
    assert entry_id_3 in [p["message_id"] for p in pending]
