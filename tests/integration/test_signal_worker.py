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
