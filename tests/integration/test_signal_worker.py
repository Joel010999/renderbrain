"""
tests/integration/test_signal_worker.py

Tests de integración para SignalWorker (S4.2).

Flujo verificado:
    RawSignalDetected (manual)
        → wrap_and_publish() → Redis Stream
        → RedisConsumerGroup.read_new() → (entry_id, EventEnvelope)
        → SignalWorker.process_one(entry_id, envelope)
            → run_signal_flow()   → CanonicalSignal (flush)
            → run_cognitive_flow() → KnowledgeTransaction | None (commit)
        → XACK ✅

Requisito previo: contenedores renderbrain-postgres y renderbrain-redis corriendo.

    docker compose up -d
    uv run pytest tests/integration/test_signal_worker.py -v -m integration

Casos verificados:
    1. Happy path (relevant=True): BD contiene CanonicalSignal + Evidence + Insight +
       KnowledgeTransaction → XACK → mensaje ya no pending.
    2. Failure: error forzado → rollback → NO XACK → mensaje pending en PEL.
    3. Relevant=False: CanonicalSignal persistida → NO KnowledgeTransaction → XACK.
    4. ID separation: Redis Entry ID ≠ envelope.event_id;
       CanonicalSignal.source_event_id == envelope.event_id.

Garantías sobre APIs externas:
    - FakeLLMProvider: NO llama a OpenAI.
    - NO llama a Apify.
    - Los tests @pytest.mark.external permanecen skipped sin --run-external.
"""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.engines.sensors import ManualSensor
from runtime.events import RedisConsumerGroup, RedisEventBus, wrap_and_publish
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.models.knowledge import (
    EvidenceModel,
    InsightModel,
    KnowledgeTransactionModel,
)
from runtime.infrastructure.database.repositories.canonical_signal import (
    CanonicalSignalRepository,
)
from runtime.infrastructure.database.models.mission import MissionModel, ProcessedSignalModel
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.infrastructure.redis.client import get_redis_client
from runtime.workers.signal_worker import SignalWorker
from tests.fakes.fake_llm_provider import FakeLLMProvider

# Streams aislados para S4.2 — no colisionan con producción ni otros tests
_TEST_STREAM = "renderbrain:test:events:s4.2"
_TEST_GROUP = "test-signal-workers"
_TEST_CONSUMER = "test-worker-1"

# Contexto de misión fijo inyectado externamente (sin retrieval de DB)
_MISSION_CONTEXT = "Test mission context for S4.2 integration tests"


# ---------------------------------------------------------------------------
# Helpers de limpieza
# ---------------------------------------------------------------------------


async def _cleanup_canonical(session_factory, canonical_id: UUID) -> None:
    from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
    from runtime.infrastructure.database.models.knowledge import EvidenceModel, InsightModel, KnowledgeTransactionModel, PatternModel, OpportunityModel, pattern_insights, opportunity_patterns
    from sqlalchemy import delete, select
    
    async with session_factory() as session:
        await session.execute(delete(opportunity_patterns))
        await session.execute(delete(OpportunityModel))
        await session.execute(delete(pattern_insights))
        await session.execute(delete(PatternModel))
        
        # Original cleanup
        stmt = select(EvidenceModel.id).where(EvidenceModel.canonical_signal_id == canonical_id)
        result = await session.execute(stmt)
        ev_ids = result.scalars().all()
        if ev_ids:
            await session.execute(delete(KnowledgeTransactionModel).where(KnowledgeTransactionModel.evidence_id.in_(ev_ids)))
            await session.execute(delete(InsightModel).where(InsightModel.evidence_id.in_(ev_ids)))
        await session.execute(delete(EvidenceModel).where(EvidenceModel.canonical_signal_id == canonical_id))
        await session.execute(delete(CanonicalSignalModel).where(CanonicalSignalModel.id == canonical_id))
        await session.commit()


async def _publish_test_signal(
    redis,
    stream: str,
    mission_id: UUID,
    body: str = "Test signal body for S4.2 worker tests.",
    fingerprint_id: str | None = None,
) -> tuple[str, "RedisEventBus"]:
    """
    Publica una señal manual en Redis y retorna bus.
    El payload incluye fingerprint_id explícito para que compute_fingerprint()
    pueda calcular la identidad estable (requerido desde S4.3).
    """
    fp_id = fingerprint_id or f"s42-test-{str(mission_id)[:8]}"
    raw_payload = {
        "body": body,
        "author": "TestSuite S4.2",
        "language": "en",
        "metrics": {"likes": 1, "reach": 10},
        "fingerprint_id": fp_id,  # requerido desde S4.3 para source='manual_input'
    }
    sensor = ManualSensor(mission_id=mission_id, raw_payload=raw_payload)
    signal: RawSignalDetected = await sensor.detect()

    bus = RedisEventBus(redis_client=redis, stream=stream)
    await wrap_and_publish(signal, bus)
    return bus


async def _create_mission_for_test(session_factory, mission_id: UUID) -> None:
    """Crea una Mission en BD para satisfacer FK de ProcessedSignal."""
    from runtime.contracts.mission import Mission
    mission = Mission(
        id=mission_id,
        name=f"S4.2 Test Mission {str(mission_id)[:8]}",
        source="manual_input",
        target="test_target",
        interval_seconds=60,
    )
    async with session_factory() as session:
        repo = MissionRepository(session)
        await repo.save(mission)
        await session.commit()


async def _cleanup_processed_signal(session_factory, mission_id: UUID) -> None:
    from runtime.infrastructure.database.models.mission import MissionModel, ProcessedSignalModel
    from runtime.infrastructure.database.models.knowledge import OpportunityModel, PatternModel, pattern_insights, opportunity_patterns
    from sqlalchemy import delete
    async with session_factory() as session:
        await session.execute(delete(ProcessedSignalModel).where(ProcessedSignalModel.mission_id == mission_id))
        
        # Cleanup opportunities and patterns
        await session.execute(delete(opportunity_patterns))
        await session.execute(delete(OpportunityModel).where(OpportunityModel.mission_id == mission_id))
        await session.execute(delete(pattern_insights))
        await session.execute(delete(PatternModel).where(PatternModel.mission_id == mission_id))
        
        await session.execute(delete(MissionModel).where(MissionModel.id == mission_id))
        await session.commit()


# ---------------------------------------------------------------------------
# Test 1 — Happy path: señal relevante
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_signal_worker_happy_path_relevant():
    """
    Caso Principal: RawSignalDetected → Redis → Worker (FakeLLM relevant=True)
    → BD contiene CanonicalSignal + Evidence + Insight + KnowledgeTransaction
    → XACK → mensaje ya no está pending.

    Verifica:
    - CanonicalSignal persistida en PostgreSQL.
    - KnowledgeTransaction con Evidence e Insight persistidos.
    - Mensaje ya no está en la PEL después del XACK.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()
    canonical_id: UUID | None = None

    try:
        # 0. Crear Mission en BD (requerido por FK de ProcessedSignal desde S4.3)
        await _create_mission_for_test(async_session, mission_id)

        # 1. Publicar señal en Redis (con fingerprint_id único para este test)
        bus = await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="s42-happy-path-relevant",
        )

        # 2. Configurar consumer group
        cg = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_TEST_CONSUMER,
        )
        await cg.ensure_group()

        # 3. Leer el mensaje nuevo
        messages = await cg.read_new(count=1)
        assert len(messages) == 1, f"Expected 1 message, got {len(messages)}"
        entry_id, envelope = messages[0]

        # Verificar separación de IDs (Test 4 integrado aquí)
        assert entry_id != str(envelope.event_id), (
            "Redis Entry ID debe ser distinto del EventEnvelope.event_id"
        )

        # 4. Configurar FakeLLMProvider con respuesta relevant=True
        fake_response = json.dumps({
            "relevant": True,
            "evidence": "Signal contains clear performance data for the mission.",
            "insight": "Engagement metrics indicate growing audience interest.",
            "confidence": 0.92,
            "reason": "Directly related to mission KPIs.",
        })
        llm = FakeLLMProvider(fake_response)
        engine = CognitiveEngine(llm)

        # 5. Construir y ejecutar el worker
        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )
        canonical, transaction = await worker.process_one(entry_id, envelope)

        # 6. Verificar retornos en memoria
        assert canonical is not None
        assert canonical.source_event_id == envelope.event_id, (
            f"source_event_id debe ser igual al event_id del envelope.\n"
            f"  envelope.event_id       = {envelope.event_id}\n"
            f"  canonical.source_event_id = {canonical.source_event_id}"
        )
        assert canonical.mission_id == mission_id
        assert transaction is not None
        assert transaction.mission_id == mission_id
        canonical_id = canonical.id

        # 7. Leer de vuelta de PostgreSQL — BD debe tener todo persistido
        async with async_session() as session:
            # 7a. CanonicalSignal
            sig_repo = CanonicalSignalRepository(session)
            recovered_signal = await sig_repo.get_by_id(canonical_id)
            assert recovered_signal is not None, "CanonicalSignal debe estar en la BD"
            assert recovered_signal.source_event_id == envelope.event_id
            assert recovered_signal.mission_id == mission_id

            # 7b. KnowledgeTransaction con Evidence e Insight
            know_repo = KnowledgeCoreRepository(session)
            recovered_tx = await know_repo.get_by_id(transaction.id)
            assert recovered_tx is not None, "KnowledgeTransaction debe estar en la BD"
            assert recovered_tx.evidence.canonical_signal_id == canonical_id
            assert recovered_tx.evidence.content == "Signal contains clear performance data for the mission."
            assert recovered_tx.insight.content == "Engagement metrics indicate growing audience interest."
            assert recovered_tx.insight.evidence_id == recovered_tx.evidence.id

        # 8. Verificar XACK: el mensaje no debe estar pending
        pending_info = await redis.xpending_range(
            _TEST_STREAM, _TEST_GROUP, "-", "+", 10
        )
        pending_entry_ids = [p["message_id"] for p in pending_info]
        assert entry_id not in pending_entry_ids, (
            f"El mensaje {entry_id} debe haber sido XACK'd y no estar pending"
        )

    finally:
        if canonical_id is not None:
            await _cleanup_canonical(async_session, canonical_id)
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 2 — Failure: error forzado → rollback → NO XACK → pending
# ---------------------------------------------------------------------------


class _ExplodingLLMProvider:
    """LLMProvider que siempre lanza una excepción — simula fallo del LLM."""

    async def complete(self, prompt: str) -> str:  # noqa: ARG002
        raise RuntimeError("Simulated LLM failure for S4.2 test")


@pytest.mark.integration
async def test_signal_worker_failure_no_xack():
    """
    Caso Failure: fallo forzado en el LLM antes del commit.
    → Rollback de BD → NO XACK → mensaje sigue pending → excepción propagada.

    Verifica:
    - La excepción se propaga fuera de process_one().
    - El CanonicalSignal NO está en la BD (rollback efectivo).
    - El mensaje sigue en la PEL (no fue XACK'd).
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()

    try:
        # 0. Crear Mission en BD (requerido por FK de ProcessedSignal desde S4.3)
        await _create_mission_for_test(async_session, mission_id)

        # 1. Publicar señal (con fingerprint_id único para este test)
        await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="s42-failure-no-xack",
        )

        # 2. Consumer group
        cg = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_TEST_CONSUMER,
        )
        await cg.ensure_group()

        # 3. Leer mensaje
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        # 4. Engine con LLM que explota
        llm = _ExplodingLLMProvider()  # type: ignore[arg-type]
        engine = CognitiveEngine(llm)

        # 5. Worker — debe lanzar excepción
        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )

        with pytest.raises(RuntimeError, match="Simulated LLM failure"):
            await worker.process_one(entry_id, envelope)

        # 6. CanonicalSignal NO debe estar en BD (rollback efectivo)
        async with async_session() as session:
            result = await session.execute(
                select(CanonicalSignalModel).where(
                    CanonicalSignalModel.source_event_id == envelope.event_id
                )
            )
            assert result.scalar_one_or_none() is None, (
                "CanonicalSignal no debe estar en la BD tras rollback"
            )

        # 7. El mensaje DEBE seguir pending (no fue XACK'd)
        pending_info = await redis.xpending_range(
            _TEST_STREAM, _TEST_GROUP, "-", "+", 10
        )
        pending_entry_ids = [p["message_id"] for p in pending_info]
        assert entry_id in pending_entry_ids, (
            f"El mensaje {entry_id} debe seguir pending porque NO se hizo XACK"
        )

    finally:
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 3 — Relevant=False: CanonicalSignal guardada, NO KnowledgeTransaction
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_signal_worker_relevant_false():
    """
    Caso Relevant=False: FakeLLM retorna relevant=false.
    → CanonicalSignal se persiste correctamente.
    → NO se crea KnowledgeTransaction.
    → XACK correcto.
    → Mensaje ya no pending.

    Verifica que relevant=False no es un error:
    - process_one() retorna (canonical, None) sin lanzar excepción.
    - CanonicalSignal está en BD con la trazabilidad correcta.
    - Ninguna Evidence ni Insight fueron creados.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()
    canonical_id: UUID | None = None

    try:
        # 0. Crear Mission en BD (requerido por FK de ProcessedSignal desde S4.3)
        await _create_mission_for_test(async_session, mission_id)

        # 1. Publicar señal (con fingerprint_id único para este test)
        await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="s42-relevant-false",
        )

        # 2. Consumer group
        cg = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_TEST_CONSUMER,
        )
        await cg.ensure_group()

        # 3. Leer mensaje
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        # 4. FakeLLM con relevant=False
        fake_response = json.dumps({
            "relevant": False,
            "evidence": None,
            "insight": None,
            "confidence": None,
            "reason": "Signal not related to the mission objectives.",
        })
        llm = FakeLLMProvider(fake_response)
        engine = CognitiveEngine(llm)

        # 5. Worker
        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )
        canonical, transaction = await worker.process_one(entry_id, envelope)

        # 6. Verificar: transaction debe ser None (no es un error)
        assert canonical is not None
        assert transaction is None, (
            "KnowledgeTransaction debe ser None cuando relevant=False"
        )
        canonical_id = canonical.id

        # 7. CanonicalSignal DEBE estar en BD
        async with async_session() as session:
            sig_repo = CanonicalSignalRepository(session)
            recovered = await sig_repo.get_by_id(canonical_id)
            assert recovered is not None, (
                "CanonicalSignal debe estar persistida aunque la señal no sea relevante"
            )
            assert recovered.source_event_id == envelope.event_id
            assert recovered.mission_id == mission_id

            # Evidence e Insight NO deben existir
            ev_result = await session.execute(
                select(EvidenceModel).where(EvidenceModel.canonical_signal_id == canonical_id)
            )
            assert ev_result.scalar_one_or_none() is None, (
                "No debe existir Evidence cuando relevant=False"
            )

        # 8. Mensaje NO debe estar pending (XACK fue correcto)
        pending_info = await redis.xpending_range(
            _TEST_STREAM, _TEST_GROUP, "-", "+", 10
        )
        pending_entry_ids = [p["message_id"] for p in pending_info]
        assert entry_id not in pending_entry_ids, (
            f"El mensaje {entry_id} debe haber sido XACK'd (relevant=False no es error)"
        )

    finally:
        if canonical_id is not None:
            await _cleanup_canonical(async_session, canonical_id)
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 4 — ID Separation: Redis Entry ID ≠ event_id; source_event_id == event_id
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_signal_worker_id_separation():
    """
    Test de separación de IDs (S4.2 criterio de salida explícito).

    Verifica:
    - Redis Entry ID (posición técnica) ≠ EventEnvelope.event_id (UUID de negocio).
    - CanonicalSignal.source_event_id == EventEnvelope.event_id (trazabilidad).
    - El XACK usa el Redis Entry ID, no el event_id.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()
    canonical_id: UUID | None = None

    try:
        # 0. Crear Mission en BD (requerido por FK de ProcessedSignal desde S4.3)
        await _create_mission_for_test(async_session, mission_id)

        # 1. Publicar señal (con fingerprint_id único para este test)
        await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="s42-id-separation",
        )

        # 2. Consumer group
        cg = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_TEST_CONSUMER,
        )
        await cg.ensure_group()

        # 3. Leer mensaje — obtener entry_id y envelope
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        # Aserción 1: Redis Entry ID ≠ EventEnvelope.event_id
        assert entry_id != str(envelope.event_id), (
            f"Redis Entry ID debe ser distinto del event_id.\n"
            f"  entry_id   = {entry_id!r}\n"
            f"  event_id   = {str(envelope.event_id)!r}"
        )

        # 4. Procesar con FakeLLM relevant=True
        fake_response = json.dumps({
            "relevant": True,
            "evidence": "ID separation test evidence.",
            "insight": "ID separation test insight.",
            "confidence": 0.8,
            "reason": "Testing ID separation.",
        })
        llm = FakeLLMProvider(fake_response)
        engine = CognitiveEngine(llm)
        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )
        canonical, transaction = await worker.process_one(entry_id, envelope)
        canonical_id = canonical.id

        # Aserción 2: CanonicalSignal.source_event_id == EventEnvelope.event_id
        assert canonical.source_event_id == envelope.event_id, (
            f"source_event_id debe ser el event_id del envelope.\n"
            f"  envelope.event_id          = {envelope.event_id}\n"
            f"  canonical.source_event_id  = {canonical.source_event_id}"
        )

        # Aserción 3: Verificar en BD que la trazabilidad persiste
        async with async_session() as session:
            sig_repo = CanonicalSignalRepository(session)
            recovered = await sig_repo.get_by_id(canonical_id)
            assert recovered is not None
            assert recovered.source_event_id == envelope.event_id, (
                "La trazabilidad source_event_id debe persistir en la BD"
            )

    finally:
        if canonical_id is not None:
            await _cleanup_canonical(async_session, canonical_id)
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 5 — Crash Recovery with REAL Redelivery
# ---------------------------------------------------------------------------

class _StatefulLLMProvider:
    def __init__(self, fail_first: bool = True):
        self.fail_first = fail_first
        self.call_count = 0

    async def complete(self, prompt: str) -> str:
        self.call_count += 1
        
        # Determine what is being asked
        if "relevant: true/false" in prompt:
            return '''{"relevant": true, "evidence": "Crash evidence.", "insight": "Crash insight.", "confidence": 0.9, "reason": "test"}'''
        if "pattern_found" in prompt:
            return '''{"pattern_found": true, "content": "A crash pattern", "confidence": 0.9, "supporting_insight_indexes": [0, 1], "reason": "test"}'''
        if "opportunity_found" in prompt:
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("Simulated crash in OpportunityDetector")
            return '''{"opportunity_found": true, "content": "A crash opportunity", "confidence": 0.9, "supporting_pattern_indexes": [0], "reason": "test"}'''
            
        return "{}"

@pytest.mark.integration
async def test_signal_worker_crash_and_redelivery():
    """
    Test 5: Crash recovery and real redelivery.
    
    Primera ejecución: falla en el LLM (simulando PatternDetector crash/etc).
    Segunda ejecución: lee mensaje pending (XAUTOCLAIM/read_pending), LLM ok, 1 artifact de c/u.
    Tercera ejecución: simulación de redelivery en Redis. Dedupe temprano lo descarta. LLM call count no sube.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()
    canonical_id: UUID | None = None

    try:
        await _create_mission_for_test(async_session, mission_id)
        
        # Insert 2 dummy insights to trigger PatternDetector on the next signal
        from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
        from runtime.infrastructure.database.models.knowledge import EvidenceModel, InsightModel, KnowledgeTransactionModel
        from datetime import datetime, UTC
        
        
        async with async_session() as session:
            for i in range(2):
                c_id = uuid4()
                e_id = uuid4()
                i_id = uuid4()
                session.add(CanonicalSignalModel(
                    id=c_id, mission_id=mission_id, source='dummy', sensor='dummy',
                    source_event_id=uuid4(), metrics={'k': 'v'}, content=f'Dummy signal {i}',
                    captured_at=datetime.now(UTC), normalized_at=datetime.now(UTC)
                ))
                session.add(EvidenceModel(
                    id=e_id, mission_id=mission_id, canonical_signal_id=c_id,
                    content=f'Dummy evidence {i}', created_at=datetime.now(UTC)
                ))
                session.add(InsightModel(
                    id=i_id, mission_id=mission_id, evidence_id=e_id,
                    content=f'Dummy insight {i}', created_at=datetime.now(UTC)
                ))
            
            # Insert a dummy pattern so OpportunityDetector will actually query the LLM and trigger the simulated crash
            from runtime.infrastructure.database.models.knowledge import PatternModel
            p_id = uuid4()
            session.add(PatternModel(
                id=p_id, mission_id=mission_id, content="Dummy Pattern", confidence=0.9,
                support_count=2, created_at=datetime.now(UTC)
            ))
            await session.commit()
        
        # 1. Publish test signal

        bus = await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="s42-crash-redelivery",
        )
        
        cg = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_TEST_CONSUMER,
        )
        await cg.ensure_group()
        
        # 2. First Execution (Crash)
        llm = _StatefulLLMProvider(fail_first=True)
        engine = CognitiveEngine(llm) # type: ignore
        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )
        
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]
        
        with pytest.raises(RuntimeError, match="Simulated crash"):
            await worker.process_one(entry_id, envelope)
            
        assert llm.call_count == 3
        
        # Check BD: empty
        async with async_session() as session:
            canonical_repo = CanonicalSignalRepository(session)
            canonical_models = await session.execute(select(CanonicalSignalModel).where(CanonicalSignalModel.source_event_id == envelope.event_id))
            canonical = canonical_models.scalar_one_or_none()
            assert canonical is None, "Should be rollback"
            
            # verify pending
            pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
            assert entry_id in [p["message_id"] for p in pending]

        # 3. Second Execution (Redelivery from Pending - Success)
        results = await worker.process_next(count=1)
        assert len(results) == 1
        canonical, transaction = results[0]
        
        assert canonical is not None
        canonical_id = canonical.id
        assert transaction is not None
        assert llm.call_count == 6
        
        async with async_session() as session:
            # Verify 1 element exists
            sig_repo = CanonicalSignalRepository(session)
            recovered = await sig_repo.get_by_id(canonical_id)
            assert recovered is not None
            
            # Message is not pending
            pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
            assert entry_id not in [p["message_id"] for p in pending]

        # 4. Third Execution (Simulated Redelivery in Redis of already processed message)
        # We manually push a new message with the SAME payload (same fingerprint)
        await wrap_and_publish(RawSignalDetected.model_validate(envelope.payload), bus)
        
        new_messages = await cg.read_new(count=1)
        assert len(new_messages) == 1
        new_entry_id, new_envelope = new_messages[0]
        assert new_entry_id != entry_id # New Redis id
        
        # Process third time
        res = await worker.process_one(new_entry_id, new_envelope)
        # dedupe kicks in early: result is (None, None)
        assert res == (None, None)
        # LLM NOT called again!
        assert llm.call_count == 6
        
        # Message is acked
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert new_entry_id not in [p["message_id"] for p in pending]
            
    finally:
        if canonical_id is not None:
            await _cleanup_canonical(async_session, canonical_id)
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Hotfix v1.0.2 — Regression Tests: Resilient Opportunity Support Mapping
# ---------------------------------------------------------------------------


class _MultiStepFakeLLM:
    """
    LLM fake que responde diferente segun el prompt recibido.
    Permite simular el comportamiento completo del pipeline cognitivo:
      - CognitiveEngine  -> relevant=True
      - PatternDetector  -> pattern_found=False (sin umbral, sin pattern nuevo)
      - OpportunityDetector -> configurable por el caller
    """

    def __init__(self, opportunity_response=None, infra_error=None):
        self.opportunity_response = opportunity_response
        self.infra_error = infra_error
        self.call_count = 0

    async def complete(self, prompt):
        self.call_count += 1

        # CognitiveEngine prompt
        if "evidence" in prompt and "insight" in prompt and "relevant" in prompt:
            return json.dumps({
                "relevant": True,
                "evidence": "Evidence for hotfix regression test",
                "insight": "Insight for hotfix regression test",
                "confidence": 0.85,
                "reason": "Hotfix v1.0.2 regression test",
            })

        # PatternDetector prompt
        if "pattern_found" in prompt or "Un patron es una recurrencia" in prompt:
            return json.dumps({
                "pattern_found": False,
                "reason": "Not enough insights for hotfix regression test",
            })

        # OpportunityDetector prompt
        if "opportunity_found" in prompt or "Oportunidad" in prompt:
            if self.infra_error is not None:
                raise self.infra_error
            if self.opportunity_response is not None:
                return self.opportunity_response
            return json.dumps({
                "opportunity_found": False,
                "reason": "No opportunity found in hotfix regression test",
            })

        # Default fallback
        return json.dumps({"relevant": False, "reason": "Unknown prompt"})


@pytest.mark.integration
async def test_invalid_opportunity_support_preserves_intelligence():
    """
    HOTFIX v1.0.2 -- Test de Regresion Exacto.

    Reproduce el bug de produccion:
    - 1 Pattern pre-cargado en la vista de la mision.
    - LLM fake retorna supporting_pattern_indexes: [2] (invalido: fuera de rango).

    Resultado esperado:
    - InvalidOpportunitySupportError es capturada internamente por el worker.
    - La Opportunity NO se persiste.
    - Insight y ProcessedSignal SI se persisten (commit exitoso).
    - Ocurre XACK -> el mensaje NO queda pending.
    """
    from runtime.infrastructure.database.models.knowledge import (
        InsightModel,
        OpportunityModel,
    )

    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()
    canonical_id = None

    try:
        await _create_mission_for_test(async_session, mission_id)

        async with async_session() as session:
            from runtime.infrastructure.database.models.knowledge import PatternModel
            from datetime import datetime, UTC as _UTC
            session.add(PatternModel(
                id=uuid4(),
                mission_id=mission_id,
                content="Pre-loaded pattern for hotfix regression",
                confidence=0.9,
                support_count=2,
                created_at=datetime.now(_UTC),
            ))
            await session.commit()

        await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="hotfix-v102-invalid-support",
        )

        cg = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_TEST_CONSUMER,
        )
        await cg.ensure_group()

        invalid_opp_response = json.dumps({
            "opportunity_found": True,
            "content": "Opportunity with invalid index",
            "confidence": 0.8,
            "supporting_pattern_indexes": [2],
            "reason": "Regression test for hotfix v1.0.2",
        })
        llm = _MultiStepFakeLLM(opportunity_response=invalid_opp_response)
        engine = CognitiveEngine(llm)

        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )

        canonical, transaction = await worker.process_one(entry_id, envelope)
        assert canonical is not None
        canonical_id = canonical.id

        async with async_session() as session:
            sig_repo = CanonicalSignalRepository(session)
            recovered = await sig_repo.get_by_id(canonical_id)
            assert recovered is not None, "CanonicalSignal DEBE estar persistida"

            ps_result = await session.execute(
                select(ProcessedSignalModel).where(ProcessedSignalModel.mission_id == mission_id)
            )
            assert len(ps_result.scalars().all()) == 1, "ProcessedSignal DEBE estar persistida"

            insight_result = await session.execute(
                select(InsightModel).where(InsightModel.mission_id == mission_id)
            )
            assert len(insight_result.scalars().all()) == 1, "Insight DEBE estar persistido"

            opp_result = await session.execute(
                select(OpportunityModel).where(OpportunityModel.mission_id == mission_id)
            )
            assert len(opp_result.scalars().all()) == 0, "Opportunity NO debe persistirse con indice invalido"

        pending_info = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id not in [p["message_id"] for p in pending_info], "Mensaje DEBE haber sido XACK'd"

    finally:
        if canonical_id is not None:
            await _cleanup_canonical(async_session, canonical_id)
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


@pytest.mark.integration
async def test_valid_opportunity_support_persists():
    """
    HOTFIX v1.0.2 -- Test de Indice Correcto.
    1 Pattern pre-cargado. LLM fake retorna supporting_pattern_indexes: [0].
    Resultado: Opportunity persiste correctamente.
    """
    from runtime.infrastructure.database.models.knowledge import OpportunityModel

    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()
    canonical_id = None

    try:
        await _create_mission_for_test(async_session, mission_id)

        async with async_session() as session:
            from runtime.infrastructure.database.models.knowledge import PatternModel
            from datetime import datetime, UTC as _UTC
            session.add(PatternModel(
                id=uuid4(),
                mission_id=mission_id,
                content="Pre-loaded pattern for valid opportunity test",
                confidence=0.9,
                support_count=2,
                created_at=datetime.now(_UTC),
            ))
            await session.commit()

        await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="hotfix-v102-valid-support",
        )

        cg = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_TEST_CONSUMER,
        )
        await cg.ensure_group()

        valid_opp_response = json.dumps({
            "opportunity_found": True,
            "content": "Valid opportunity supported by pattern [0]",
            "confidence": 0.87,
            "supporting_pattern_indexes": [0],
            "reason": "Valid index test for hotfix v1.0.2",
        })
        llm = _MultiStepFakeLLM(opportunity_response=valid_opp_response)
        engine = CognitiveEngine(llm)

        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )
        canonical, transaction = await worker.process_one(entry_id, envelope)
        canonical_id = canonical.id

        async with async_session() as session:
            opp_result = await session.execute(
                select(OpportunityModel).where(OpportunityModel.mission_id == mission_id)
            )
            opps = opp_result.scalars().all()
            assert len(opps) == 1, f"Opportunity DEBE persistirse con indice valido [0]. Encontradas: {len(opps)}"
            assert opps[0].content == "Valid opportunity supported by pattern [0]"

        pending_info = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id not in [p["message_id"] for p in pending_info]

    finally:
        if canonical_id is not None:
            await _cleanup_canonical(async_session, canonical_id)
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


@pytest.mark.integration
async def test_infra_error_in_opportunity_causes_rollback_no_xack():
    """
    HOTFIX v1.0.2 -- Test de Error de Infraestructura.
    OpportunityDetector lanza RuntimeError (timeout real del provider).
    Resultado: Rollback completo, NO XACK, mensaje pending.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()

    try:
        await _create_mission_for_test(async_session, mission_id)

        async with async_session() as session:
            from runtime.infrastructure.database.models.knowledge import PatternModel
            from datetime import datetime, UTC as _UTC
            session.add(PatternModel(
                id=uuid4(),
                mission_id=mission_id,
                content="Pre-loaded pattern for infra error test",
                confidence=0.9,
                support_count=2,
                created_at=datetime.now(_UTC),
            ))
            await session.commit()

        await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="hotfix-v102-infra-error",
        )

        cg = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_TEST_CONSUMER,
        )
        await cg.ensure_group()

        infra_error = RuntimeError("Simulated provider timeout in OpportunityDetector")
        llm = _MultiStepFakeLLM(infra_error=infra_error)
        engine = CognitiveEngine(llm)

        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )

        with pytest.raises(RuntimeError, match="Simulated provider timeout"):
            await worker.process_one(entry_id, envelope)

        async with async_session() as session:
            cs_result = await session.execute(
                select(CanonicalSignalModel).where(CanonicalSignalModel.source_event_id == envelope.event_id)
            )
            assert cs_result.scalar_one_or_none() is None, "CanonicalSignal NO debe estar en BD tras rollback"

        pending_info = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id in [p["message_id"] for p in pending_info], "Mensaje DEBE seguir pending -- NO XACK"

    finally:
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test: pending recovery after dead consumer
#
# Escenario productivo exacto:
#   Consumer A lee un mensaje → falla (LLM error) → mensaje queda pending bajo A
#   Consumer A desaparece (redeploy)
#   Consumer B arranca con distinto consumer_name
#   Consumer B llama process_next() → XAUTOCLAIM reclama el pending de A (min_idle=0)
#   → procesa → XACK → mensaje ya no pending
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pending_recovery_after_dead_consumer():
    """
    Consumer A (diferente nombre) recibe mensaje → falla → pending.
    Consumer B arranca → process_next() → XAUTOCLAIM reclama el pending de A
    → lo procesa → XACK.

    Verifica:
    - XAUTOCLAIM reclama de CUALQUIER consumer del grupo, no solo del propio.
    - process_next() recupera y procesa el mensaje huérfano.
    - Después del XACK, el mensaje ya no está pending.
    - CanonicalSignal persiste en BD (procesamiento exitoso).
    - Los logs de recovery son visibles: 'pending recovery scan', 'pending found',
      'pending reclaimed', 'recovering pending message'.
    """
    _CONSUMER_A = "dead-consumer-A"
    _CONSUMER_B = "alive-consumer-B"

    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()
    canonical_id = None

    try:
        # 0. Crear Mission en BD
        await _create_mission_for_test(async_session, mission_id)

        # 1. Publicar señal
        await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="recovery-dead-consumer-test",
        )

        # 2. Consumer A — lee el mensaje (entra en su PEL) pero no procesa
        cg_a = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_CONSUMER_A,
        )
        await cg_a.ensure_group()

        messages = await cg_a.read_new(count=1)
        assert len(messages) == 1, "Consumer A debe leer 1 mensaje"
        entry_id, envelope = messages[0]

        # Verificar que el mensaje está pending bajo Consumer A
        pending_before = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id in [p["message_id"] for p in pending_before], (
            "El mensaje debe estar pending bajo Consumer A"
        )
        owner_before = next(
            (p["consumer"] for p in pending_before if p["message_id"] == entry_id), None
        )
        assert owner_before == _CONSUMER_A, (
            f"El owner debe ser Consumer A. Got: {owner_before}"
        )

        # 3. Consumer A "muere" — simplemente no procesa ni ACK el mensaje.

        # 4. Consumer B arranca — usa process_next() con FakeLLM que funciona
        fake_response = json.dumps({
            "relevant": True,
            "evidence": "Evidence from pending recovery test.",
            "insight": "Insight from pending recovery test.",
            "confidence": 0.85,
            "reason": "Pending recovery after dead consumer.",
        })
        llm_b = FakeLLMProvider(fake_response)
        engine_b = CognitiveEngine(llm_b)

        cg_b = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_CONSUMER_B,
        )
        worker_b = SignalWorker(
            consumer_group=cg_b,
            session_factory=async_session,
            cognitive_engine=engine_b,
            llm_provider=llm_b,
            mission_context=_MISSION_CONTEXT,
        )

        # 5. Consumer B llama process_next() → debe reclamar pending de A via XAUTOCLAIM
        results = await worker_b.process_next(count=10)

        assert len(results) == 1, (
            f"Consumer B debe recuperar 1 mensaje pending de Consumer A. Got: {len(results)}"
        )
        canonical, transaction = results[0]

        # 6. Verificar resultado del procesamiento
        assert canonical is not None, "CanonicalSignal debe estar presente tras recovery"
        assert transaction is not None, "KnowledgeTransaction debe existir (relevant=True)"
        canonical_id = canonical.id

        # 7. Verificar persistencia en BD
        async with async_session() as session:
            sig_repo = CanonicalSignalRepository(session)
            recovered = await sig_repo.get_by_id(canonical_id)
            assert recovered is not None, "CanonicalSignal debe estar en BD después de recovery"
            assert recovered.mission_id == mission_id

        # 8. Verificar XACK — mensaje ya no pending
        pending_after = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        pending_ids_after = [p["message_id"] for p in pending_after]
        assert entry_id not in pending_ids_after, (
            f"El mensaje {entry_id} debe haber sido XACK'd por Consumer B. "
            f"Pending restantes: {pending_ids_after}"
        )

    finally:
        if canonical_id is not None:
            await _cleanup_canonical(async_session, canonical_id)
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test: provider failure keeps pending — mensaje sigue retryable
#
# Escenario:
#   Consumer A lee mensaje → queda pending (sin procesar)
#   Consumer B intenta recovery → LLM sigue fallando → rollback → NO XACK
#   → mensaje sigue pending (retryable)
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_pending_stays_pending_on_provider_failure():
    """
    Consumer A no procesa → mensaje pending.
    Consumer B intenta recovery → provider sigue fallando → rollback → NO XACK.
    → Mensaje SIGUE pending (retryable en el siguiente ciclo).

    Verifica:
    - Si el provider sigue fallando durante recovery, el mensaje NO se pierde.
    - NO hay XACK → el mensaje permanece en la PEL.
    - El error se logea pero NO detiene el worker (process_next() absorbe el error).
    - process_next() retorna lista vacía (ningún resultado exitoso).
    - CanonicalSignal NO está en BD (rollback efectivo).
    """
    _CONSUMER_A = "dead-consumer-A-fail"
    _CONSUMER_B = "alive-consumer-B-fail"

    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()

    try:
        # 0. Crear Mission en BD
        await _create_mission_for_test(async_session, mission_id)

        # 1. Publicar señal
        await _publish_test_signal(
            redis, _TEST_STREAM, mission_id,
            fingerprint_id="recovery-provider-failure-test",
        )

        # 2. Consumer A — lee pero no procesa (simula muerte pre-procesamiento)
        cg_a = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_CONSUMER_A,
        )
        await cg_a.ensure_group()

        messages = await cg_a.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        # Verificar pending inicial
        pending_before = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id in [p["message_id"] for p in pending_before]

        # 3. Consumer B — con LLM que siempre falla (provider infra error)
        llm_b = _ExplodingLLMProvider()  # type: ignore[arg-type]
        engine_b = CognitiveEngine(llm_b)

        cg_b = RedisConsumerGroup(
            redis_client=redis,
            stream=_TEST_STREAM,
            group=_TEST_GROUP,
            consumer_name=_CONSUMER_B,
        )
        worker_b = SignalWorker(
            consumer_group=cg_b,
            session_factory=async_session,
            cognitive_engine=engine_b,
            llm_provider=llm_b,
            mission_context=_MISSION_CONTEXT,
        )

        # 4. process_next() intenta recovery → LLM falla → rollback → NO XACK
        #    process_next() absorbe la excepción internamente (logs ERROR pero no re-raise)
        results = await worker_b.process_next(count=10)

        # Ningún procesamiento exitoso
        assert len(results) == 0, (
            f"No debe haber resultados exitosos cuando el provider falla. Got: {len(results)}"
        )

        # 5. El mensaje SIGUE pending — NO fue XACK'd
        pending_after = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        pending_ids_after = [p["message_id"] for p in pending_after]
        assert entry_id in pending_ids_after, (
            f"El mensaje {entry_id} debe seguir pending cuando el provider falla. "
            f"Pending encontrados: {pending_ids_after}"
        )

        # 6. CanonicalSignal NO debe estar en BD (rollback efectivo)
        async with async_session() as session:
            from sqlalchemy import select as _select
            result = await session.execute(
                _select(CanonicalSignalModel).where(
                    CanonicalSignalModel.source_event_id == envelope.event_id
                )
            )
            assert result.scalar_one_or_none() is None, (
                "CanonicalSignal NO debe estar en BD cuando el provider falla"
            )

    finally:
        await _cleanup_processed_signal(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()
