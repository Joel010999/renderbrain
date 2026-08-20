"""
tests/integration/test_e2e_automation.py

Test automático E2E determinista de todo el ciclo de RenderBrain (S4.4).
Mission -> Scheduler -> Redis -> Worker -> DB
"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.events.bus import RedisEventBus
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.models.knowledge import KnowledgeTransactionModel
from runtime.infrastructure.database.models.mission import ProcessedSignalModel
from runtime.infrastructure.redis.client import get_redis_client
from runtime.orchestration.mission_scheduler import MissionSchedulerOrchestrator
from tests.fakes.fake_llm_provider import FakeLLMProvider
from tests.integration.test_deduplication import (
    _INSTAGRAM_PAYLOAD,
    _TEST_GROUP,
    _TEST_STREAM,
    _cleanup_all,
    _create_mission,
    _make_worker,
    _setup_consumer_group,
)
from tests.integration.test_mission_scheduler import FakeSensor, FakeSensorFactory


class CountingFakeLLM(FakeLLMProvider):
    def __init__(self, response_json: str):
        super().__init__(response_json)

    async def complete(self, prompt: str) -> str:
        if "Un patrón es una recurrencia" in prompt or "pattern_found" in prompt:
            self.call_count += 1
            return '{"pattern_found": true, "content": "Test Pattern", "confidence": 0.9, "supporting_insight_indexes": [0], "reason": "test"}'
        if "opportunity_found" in prompt or "Una oportunidad de negocio" in prompt:
            self.call_count += 1
            return '{"opportunity_found": true, "content": "Test Opportunity", "confidence": 0.9, "supporting_pattern_indexes": [0], "reason": "test"}'
        return await super().complete(prompt)



@pytest.mark.integration
async def test_e2e_automation_pipeline():
    """
    Test E2E determinista.

    Paso 1: Nueva Señal (A) -> Pasa por Scheduler -> Redis -> Worker -> Se procesa -> Se guarda.
    Paso 2: Misma Señal (A) -> Pasa por Scheduler -> Redis -> Worker -> Deduplicado (hit) -> NO LLM.
    Paso 3: Señal Distinta (B) -> Pasa por Scheduler -> Redis -> Worker -> Se procesa -> Se guarda.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id = uuid4()
    
    # Preparamos las herramientas
    bus = RedisEventBus(redis, _TEST_STREAM)
    cg = await _setup_consumer_group(redis)
    
    llm = CountingFakeLLM(
        json.dumps({
            "relevant": True,
            "evidence": "E2E Test Evidence",
            "insight": "E2E Test Insight",
            "confidence": 0.99,
            "reason": "Because E2E Test"
        })
    )
    from runtime.engines.cognitive.engine import CognitiveEngine
    from runtime.workers.signal_worker import SignalWorker
    engine = CognitiveEngine(llm)
    worker = SignalWorker(
        consumer_group=cg,
        session_factory=async_session,
        cognitive_engine=engine,
        llm_provider=llm,
        mission_context="E2E test context",
    )

    try:
        mission = await _create_mission(async_session, mission_id)

        # =========================================================================
        # PASO 1: Señal Nueva (A)
        # =========================================================================

        payload_a = dict(_INSTAGRAM_PAYLOAD)
        payload_a["data"]["id"] = "signal_A_123"

        signal_a = RawSignalDetected(
            sensor="fake",
            source="instagram",
            mission_id=mission_id,
            raw_payload=payload_a
        )
        factory_a = FakeSensorFactory(FakeSensor(raw_signal=signal_a))
        scheduler_a = MissionSchedulerOrchestrator(factory_a, bus)
        
        # 1.1 Ejecuta Scheduler
        env_a = await scheduler_a.execute_mission(mission)
        assert env_a is not None, "El scheduler debió publicar la señal A"
        assert env_a.event_id is not None
        assert env_a.payload["mission_id"] == str(mission_id)

        # 1.2 Ejecuta Worker
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id_a, read_env_a = messages[0]
        
        canonical_a, tx_a = await worker.process_one(entry_id_a, read_env_a)
        
        # 1.3 Verificaciones: Genealogía del mission_id (Criterio estricto S4.4)
        assert canonical_a is not None
        assert tx_a is not None
        assert llm.call_count == 1
        
        # Verificar en DB que la señal ProcessedSignal existe
        from sqlalchemy import select
        async with async_session() as session:
            count_ps = await session.execute(select(ProcessedSignalModel).where(ProcessedSignalModel.mission_id == mission_id))
            ps_model = count_ps.scalars().first()
            assert ps_model is not None

            count_cs = await session.execute(select(CanonicalSignalModel).where(CanonicalSignalModel.mission_id == mission_id))
            cs_model = count_cs.scalars().first()
            assert cs_model is not None
            
            count_tx = await session.execute(select(KnowledgeTransactionModel).where(KnowledgeTransactionModel.mission_id == mission_id))
            tx_model = count_tx.scalars().first()
            assert tx_model is not None
            
            # Aserción contractual principal solicitada
            assert (
                mission.id 
                == signal_a.mission_id
                == canonical_a.mission_id
                == ps_model.mission_id
                == tx_model.mission_id
            ), "Violación de la genealogía de mission_id top-level"
            
        # Verificar que ya no está pending (XACK exitoso)
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id_a not in [p["message_id"] for p in pending]

        # =========================================================================
        # PASO 2: Duplicado de la Señal (A)
        # =========================================================================
        
        # 2.1 Ejecuta Scheduler de nuevo con el mismo fake sensor
        env_a2 = await scheduler_a.execute_mission(mission)
        assert env_a2 is not None
        
        # 2.2 Ejecuta Worker
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id_a2, read_env_a2 = messages[0]
        
        canonical_a2, tx_a2 = await worker.process_one(entry_id_a2, read_env_a2)
        
        # 2.3 Verificaciones
        assert canonical_a2 is None, "Dedupe hit debe retornar None para Canonical"
        assert tx_a2 is None, "Dedupe hit debe retornar None para TX"
        assert llm.call_count == 1, "Call count NO debe aumentar en un duplicado"
        
        # Verificar BD: Ninguna entidad extra creada
        async with async_session() as session:
            count_ps = await session.execute(select(ProcessedSignalModel).where(ProcessedSignalModel.mission_id == mission_id))
            assert len(count_ps.scalars().all()) == 1

            count_cs = await session.execute(select(CanonicalSignalModel).where(CanonicalSignalModel.mission_id == mission_id))
            assert len(count_cs.scalars().all()) == 1
            
        # XACK debe haberse hecho de todos modos
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id_a2 not in [p["message_id"] for p in pending]

        # =========================================================================
        # PASO 3: Señal Distinta (B)
        # =========================================================================
        
        payload_b = dict(_INSTAGRAM_PAYLOAD)
        payload_b["data"]["id"] = "signal_B_456"

        signal_b = RawSignalDetected(
            sensor="fake",
            source="instagram",
            mission_id=mission_id,
            raw_payload=payload_b
        )
        factory_b = FakeSensorFactory(FakeSensor(raw_signal=signal_b))
        scheduler_b = MissionSchedulerOrchestrator(factory_b, bus)
        
        # 3.1 Ejecuta Scheduler
        env_b = await scheduler_b.execute_mission(mission)
        assert env_b is not None
        
        # 3.2 Ejecuta Worker
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id_b, read_env_b = messages[0]
        
        canonical_b, tx_b = await worker.process_one(entry_id_b, read_env_b)
        
        # 3.3 Verificaciones
        assert canonical_b is not None
        assert tx_b is not None
        assert llm.call_count == 2, "Call count DEBE aumentar para una nueva señal"
        
        async with async_session() as session:
            count_ps = await session.execute(select(ProcessedSignalModel).where(ProcessedSignalModel.mission_id == mission_id))
            assert len(count_ps.scalars().all()) == 2

            count_cs = await session.execute(select(CanonicalSignalModel).where(CanonicalSignalModel.mission_id == mission_id))
            assert len(count_cs.scalars().all()) == 2
            
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id_b not in [p["message_id"] for p in pending]

    finally:
        await _cleanup_all(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()
