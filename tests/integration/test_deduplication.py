"""
tests/integration/test_deduplication.py

Tests de integración para la deduplicación de señales en SignalWorker (S4.3).

Estrategia:
    - CERO llamadas a OpenAI o Apify: FakeLLMProvider + ManualSensor / payloads fake.
    - CERO dependencias de internet: solo Redis y PostgreSQL locales.
    - Cada test crea su propia Mission en BD (necesario por FK processed_signals.mission_id).
    - Limpieza completa en finally: signals, processed_signals, missions.

Pre-requisito:
    docker compose up -d
    uv run pytest tests/integration/test_deduplication.py -v -m integration

Casos cubiertos:
    1. Señal nueva (miss)          → pipeline completo → ProcessedSignal registrado → XACK.
    2. Señal duplicada (hit)       → omite flows → CERO llamadas al LLM → XACK.
    3. Crash recovery (simulado)   → mensaje pending → relectura → hit → XACK sin LLM.
    4. Race condition (UNIQUE)     → IntegrityError capturado → XACK controlado.
    5. Fingerprint distinto        → mismo source, otra mission_id → procesado OK.
    6. Fingerprint distinto        → mismo fingerprint, otro source → procesado OK.
    7. FingerprintError            → NO XACK → mensaje pending.
    8. Fingerprint determinista    → mismo post → mismo fingerprint siempre.
    9. URL Fallback sin query param → limpieza de query params funciona.
"""

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.mission import Mission
from runtime.contracts.processed_signal import ProcessedSignal
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
from runtime.infrastructure.database.models.mission import (
    MissionModel,
    ProcessedSignalModel,
)
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.infrastructure.database.repositories.processed_signal import (
    ProcessedSignalRepository,
)
from runtime.infrastructure.redis.client import get_redis_client
from runtime.workers import FingerprintError, compute_fingerprint
from runtime.workers.signal_worker import SignalWorker
from tests.fakes.fake_llm_provider import FakeLLMProvider

# ---------------------------------------------------------------------------
# Streams y grupos aislados para S4.3 — no colisionan con producción ni S4.2
# ---------------------------------------------------------------------------

_TEST_STREAM = "renderbrain:test:events:s4.3"
_TEST_GROUP = "test-signal-workers-s4.3"
_TEST_CONSUMER = "test-worker-1"
_MISSION_CONTEXT = "Test mission context for S4.3 deduplication integration tests."

# Payload de Instagram fake con ID nativo — reutilizable en varios tests
_INSTAGRAM_NATIVE_ID = "CTest43Dedup"
_INSTAGRAM_URL = "https://www.instagram.com/p/CTest43Dedup/"

_INSTAGRAM_PAYLOAD: dict[str, Any] = {
    "url_queried": _INSTAGRAM_URL,
    "items_received": 1,
    "data": {
        "id": _INSTAGRAM_NATIVE_ID,
        "shortCode": _INSTAGRAM_NATIVE_ID,
        "url": _INSTAGRAM_URL,
        "caption": "Test caption for S4.3 deduplication.",
        "likesCount": 42,
        "commentsCount": 7,
    },
}

_FAKE_LLM_RELEVANT = json.dumps({
    "relevant": True,
    "evidence": "Signal contains relevant performance data.",
    "insight": "Engagement metrics show growing interest.",
    "confidence": 0.88,
    "reason": "Directly related to mission KPIs.",
})

_FAKE_LLM_IRRELEVANT = json.dumps({
    "relevant": False,
    "evidence": None,
    "insight": None,
    "confidence": None,
    "reason": "Signal not related to the mission objectives.",
})


# ---------------------------------------------------------------------------
# Helpers de setup y limpieza
# ---------------------------------------------------------------------------


async def _create_mission(session_factory, mission_id: UUID) -> Mission:
    """
    Crea una Mission real en BD para satisfacer la FK de ProcessedSignal.

    IMPORTANTE: Los ProcessedSignal tienen FK → missions.id.
    Los tests de S4.2 usaban uuid4() sin persistir Mission, pero S4.3
    requiere Mission real para poder insertar ProcessedSignal.
    """
    mission = Mission(
        id=mission_id,
        name=f"S4.3 Test Mission {str(mission_id)[:8]}",
        source="instagram",
        target="test_target",
        interval_seconds=60,
    )
    async with session_factory() as session:
        repo = MissionRepository(session)
        await repo.save(mission)
        await session.commit()
    return mission


async def _cleanup_all(session_factory, mission_id: UUID) -> None:
    """
    Limpia todos los registros creados durante un test: cascada completa.
    Orden: KnowledgeTransaction → Insight → Evidence → ProcessedSignal
           → CanonicalSignal → Mission.
    """
    async with session_factory() as session:
        # Limpiar knowledge dependiente de CanonicalSignal de esta misión
        canon_ids_q = await session.execute(
            CanonicalSignalModel.__table__.select().where(
                CanonicalSignalModel.mission_id == mission_id
            ).with_only_columns(CanonicalSignalModel.id)
        )
        canon_ids = [row[0] for row in canon_ids_q.fetchall()]

        if canon_ids:
            ev_ids_q = await session.execute(
                EvidenceModel.__table__.select().where(
                    EvidenceModel.canonical_signal_id.in_(canon_ids)
                ).with_only_columns(EvidenceModel.id)
            )
            ev_ids = [row[0] for row in ev_ids_q.fetchall()]

            if ev_ids:
                await session.execute(
                    delete(KnowledgeTransactionModel).where(
                        KnowledgeTransactionModel.evidence_id.in_(ev_ids)
                    )
                )
                await session.execute(
                    delete(InsightModel).where(
                        InsightModel.evidence_id.in_(ev_ids)
                    )
                )
                await session.execute(
                    delete(EvidenceModel).where(EvidenceModel.id.in_(ev_ids))
                )

            await session.execute(
                delete(CanonicalSignalModel).where(
                    CanonicalSignalModel.mission_id == mission_id
                )
            )

        # ProcessedSignal
        await session.execute(
            delete(ProcessedSignalModel).where(
                ProcessedSignalModel.mission_id == mission_id
            )
        )

        # Mission
        await session.execute(
            delete(MissionModel).where(MissionModel.id == mission_id)
        )

        await session.commit()


async def _setup_consumer_group(redis, stream: str = _TEST_STREAM) -> RedisConsumerGroup:
    """Crea y configura un RedisConsumerGroup limpio para el test."""
    cg = RedisConsumerGroup(
        redis_client=redis,
        stream=stream,
        group=_TEST_GROUP,
        consumer_name=_TEST_CONSUMER,
    )
    await cg.ensure_group()
    return cg


async def _publish_instagram_signal(redis, mission_id: UUID, payload: dict | None = None) -> str:
    """Publica una señal de Instagram fake en el stream y retorna el entry_id."""
    raw_payload = payload or _INSTAGRAM_PAYLOAD
    # ManualSensor NO produce source="instagram", usamos RawSignalDetected directamente
    # porque necesitamos controlar el source para el fingerprint de Instagram.
    from runtime.contracts.event_envelope import EventEnvelope
    from runtime.events.publish_signal import wrap_and_publish

    raw_signal = RawSignalDetected(
        sensor="instagram_apify_sensor",
        source="instagram",
        mission_id=mission_id,
        raw_payload=raw_payload,
    )
    bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
    envelope = await wrap_and_publish(raw_signal, bus)
    return str(envelope.event_id)


async def _publish_manual_signal(redis, mission_id: UUID, fingerprint_id: str) -> str:
    """Publica una señal manual con fingerprint_id explícito."""
    raw_payload = {
        "body": "Test manual signal for S4.3 deduplication.",
        "author": "TestSuite S4.3",
        "language": "en",
        "fingerprint_id": fingerprint_id,
    }
    sensor = ManualSensor(mission_id=mission_id, raw_payload=raw_payload)
    signal = await sensor.detect()

    bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
    envelope = await wrap_and_publish(signal, bus)
    return str(envelope.event_id)


def _make_worker(cg: RedisConsumerGroup, fake_response: str = _FAKE_LLM_RELEVANT) -> SignalWorker:
    """Construye un SignalWorker con FakeLLMProvider."""
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)
    return SignalWorker(
        consumer_group=cg,
        session_factory=async_session,
        cognitive_engine=engine,
        llm_provider=llm,
        mission_context=_MISSION_CONTEXT,
    )


# ---------------------------------------------------------------------------
# Test 1 — Señal nueva (miss): pipeline completo + ProcessedSignal registrado
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dedup_new_signal_processes_and_registers():
    """
    Caso 1 — Señal nueva (miss):
    → Pipeline completo: signal_flow → ProcessedSignal.add → cognitive_flow → commit.
    → ProcessedSignal registrado en BD con el fingerprint correcto.
    → XACK ejecutado.
    → exists() retorna True después del commit.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id = uuid4()

    try:
        await _create_mission(async_session, mission_id)
        await _publish_instagram_signal(redis, mission_id)

        cg = await _setup_consumer_group(redis)
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        worker = _make_worker(cg)
        canonical, transaction = await worker.process_one(entry_id, envelope)

        # Resultados en memoria
        assert canonical is not None, "Señal nueva debe producir CanonicalSignal"
        assert transaction is not None, "FakeLLM relevant=True debe producir KnowledgeTransaction"

        # ProcessedSignal debe estar en BD
        async with async_session() as session:
            repo = ProcessedSignalRepository(session)
            expected_fingerprint = f"instagram:{_INSTAGRAM_NATIVE_ID}"
            assert await repo.exists(mission_id, "instagram", expected_fingerprint), (
                "ProcessedSignal debe registrarse en BD tras procesar señal nueva"
            )

        # XACK: no debe estar pending
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        assert entry_id not in [p["message_id"] for p in pending]

    finally:
        await _cleanup_all(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 2 — Señal duplicada (hit): omite flows, CERO LLM, XACK
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dedup_duplicate_skips_llm_and_xacks():
    """
    Caso 2 — Señal duplicada (hit):
    → Primera vez: procesado completo.
    → Segunda vez (mismo fingerprint): dedupe hit → NO normalizer → NO LLM → XACK.

    Verifica que FakeLLMProvider.complete() NO se llama en el segundo procesamiento.
    Verifica que NO se crea un segundo CanonicalSignal.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id = uuid4()

    try:
        await _create_mission(async_session, mission_id)

        # Publicar la MISMA señal DOS veces (mismo payload → mismo fingerprint)
        await _publish_instagram_signal(redis, mission_id)
        await _publish_instagram_signal(redis, mission_id)

        cg = await _setup_consumer_group(redis)
        messages = await cg.read_new(count=2)
        assert len(messages) == 2, "Deben leerse 2 mensajes del stream"

        # Contador de llamadas al LLM para verificar CERO en el duplicado
        call_count = [0]
        original_complete = FakeLLMProvider.complete

        class CountingFakeLLM(FakeLLMProvider):
            async def complete(self, prompt: str) -> str:
                call_count[0] += 1
                return await original_complete(self, prompt)

        llm = CountingFakeLLM(_FAKE_LLM_RELEVANT)
        engine = CognitiveEngine(llm)
        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )

        entry_id_1, envelope_1 = messages[0]
        entry_id_2, envelope_2 = messages[1]

        # Primer procesamiento — debe ser completo
        canonical_1, tx_1 = await worker.process_one(entry_id_1, envelope_1)
        assert canonical_1 is not None
        assert call_count[0] == 1, "Primer procesamiento debe llamar al LLM una vez"

        # Segundo procesamiento — debe ser omitido (hit)
        canonical_2, tx_2 = await worker.process_one(entry_id_2, envelope_2)
        assert canonical_2 is None, "Duplicado debe retornar (None, None)"
        assert tx_2 is None, "Duplicado debe retornar (None, None)"
        assert call_count[0] == 1, "Duplicado NO debe llamar al LLM (call_count sigue en 1)"

        # No debe haber un segundo CanonicalSignal en BD
        async with async_session() as session:
            result = await session.execute(
                CanonicalSignalModel.__table__.select().where(
                    CanonicalSignalModel.mission_id == mission_id
                ).with_only_columns(CanonicalSignalModel.id)
            )
            rows = result.fetchall()
            assert len(rows) == 1, (
                f"Solo debe existir 1 CanonicalSignal, encontrados: {len(rows)}"
            )

        # Ambos mensajes deben estar XACK'd
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        pending_ids = [p["message_id"] for p in pending]
        assert entry_id_1 not in pending_ids
        assert entry_id_2 not in pending_ids

    finally:
        await _cleanup_all(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 3 — Crash recovery simulado: pending → relectura → hit → XACK sin LLM
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dedup_crash_recovery_xack_without_llm():
    """
    Caso 3 — Crash Recovery simulado:
    1. Worker procesa señal → commit en BD → muere ANTES del XACK.
       (Simulado: llamamos process_one pero interceptamos el XACK)
    2. Mensaje queda pending en la PEL.
    3. Worker reiniciado: XAUTOCLAIM (read_pending) recupera el mensaje.
    4. exists() → True (ProcessedSignal ya en BD) → hit → XACK sin LLM.

    Verifica que crash recovery no causa doble procesamiento ni gasta LLM.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id = uuid4()

    try:
        await _create_mission(async_session, mission_id)
        await _publish_instagram_signal(redis, mission_id)

        cg = await _setup_consumer_group(redis)
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        # --- Fase 1: Simular commit exitoso SIN XACK ---
        # Insertamos ProcessedSignal manualmente en BD para simular que
        # el commit ocurrió pero el worker murió antes del XACK.
        fingerprint = f"instagram:{_INSTAGRAM_NATIVE_ID}"
        async with async_session() as session:
            repo = ProcessedSignalRepository(session)
            ps = ProcessedSignal(
                mission_id=mission_id,
                source="instagram",
                fingerprint=fingerprint,
            )
            await repo.add(ps)
            await session.commit()

        # Verificar que el mensaje sigue pending (no XACK'd)
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        pending_ids = [p["message_id"] for p in pending]
        assert entry_id in pending_ids, "El mensaje debe seguir pending (crash simulado)"

        # --- Fase 2: Worker reiniciado — debe detectar hit y hacer XACK ---
        call_count = [0]

        class CountingFakeLLM(FakeLLMProvider):
            async def complete(self, prompt: str) -> str:
                call_count[0] += 1
                return await FakeLLMProvider.complete(self, prompt)

        llm = CountingFakeLLM(_FAKE_LLM_RELEVANT)
        engine = CognitiveEngine(llm)
        worker = SignalWorker(
            consumer_group=cg,
            session_factory=async_session,
            cognitive_engine=engine,
            llm_provider=llm,
            mission_context=_MISSION_CONTEXT,
        )

        # Releer el mensaje pending (simula XAUTOCLAIM del reinicio)
        pending_messages = await cg.read_pending(count=1, min_idle_ms=0)
        assert len(pending_messages) == 1, "XAUTOCLAIM debe recuperar el mensaje pending"

        recovered_entry_id, recovered_envelope = pending_messages[0]
        assert recovered_entry_id == entry_id

        # Procesar: debe ser hit
        canonical, transaction = await worker.process_one(recovered_entry_id, recovered_envelope)

        assert canonical is None, "Crash recovery: hit → (None, None)"
        assert transaction is None, "Crash recovery: hit → (None, None)"
        assert call_count[0] == 0, "Crash recovery: CERO llamadas al LLM"

        # Mensaje debe estar XACK'd después de la recuperación
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        pending_ids = [p["message_id"] for p in pending]
        assert entry_id not in pending_ids, "Mensaje debe ser XACK'd tras crash recovery"

    finally:
        await _cleanup_all(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 4 — Race condition (UNIQUE constraint): IntegrityError → XACK controlado
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dedup_race_condition_integrity_error_xacks():
    """
    Caso 4 — Race condition (constraint UNIQUE violado):
    Simula que otro worker ya committeó el mismo fingerprint entre el
    exists() check y el add() flush del worker actual.

    El IntegrityError debe ser capturado limpiamente → rollback → XACK.
    No crashear, no dejar mensajes huérfanos.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id = uuid4()

    try:
        await _create_mission(async_session, mission_id)
        await _publish_instagram_signal(redis, mission_id)

        cg = await _setup_consumer_group(redis)
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        # Simular que un concurrent worker ya registró el fingerprint en BD
        # ENTRE el exists() check y el add() del worker actual.
        # Lo hacemos pre-insertando el ProcessedSignal antes de process_one().
        fingerprint = f"instagram:{_INSTAGRAM_NATIVE_ID}"
        async with async_session() as session:
            repo = ProcessedSignalRepository(session)
            ps = ProcessedSignal(
                mission_id=mission_id,
                source="instagram",
                fingerprint=fingerprint,
            )
            await repo.add(ps)
            await session.commit()

        # También necesitamos que exists() retorne False para simular la race condition:
        # En la vida real, el concurrent committeó entre exists() y add().
        # Aquí lo simulamos mockeando temporalmente exists() para que retorne False
        # mientras el ProcessedSignal ya existe en BD.

        original_exists = ProcessedSignalRepository.exists

        exists_call_count = [0]

        async def patched_exists(self, mission_id, source, fingerprint):  # noqa: ARG001
            exists_call_count[0] += 1
            # Primera llamada: simular miss (concurrent no había committeado aún)
            if exists_call_count[0] == 1:
                return False
            return await original_exists(self, mission_id, source, fingerprint)

        ProcessedSignalRepository.exists = patched_exists

        try:
            worker = _make_worker(cg)
            # process_one: exists()=False (simulado) → trata de add() → IntegrityError
            # El worker debe capturar IntegrityError → rollback → XACK → (None, None)
            canonical, transaction = await worker.process_one(entry_id, envelope)

            assert canonical is None, "Race condition: debe retornar (None, None)"
            assert transaction is None, "Race condition: debe retornar (None, None)"

            # XACK debe haberse ejecutado
            pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
            pending_ids = [p["message_id"] for p in pending]
            assert entry_id not in pending_ids, (
                "Race condition: XACK debe ejecutarse — concurrent ganó pero mensaje procesado"
            )
        finally:
            ProcessedSignalRepository.exists = original_exists

    finally:
        await _cleanup_all(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 4b — Otro IntegrityError: se propaga y NO XACK
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_dedup_other_integrity_error_no_xack():
    """
    Caso 4b — Otro IntegrityError (ej. FK violation, NOT NULL, o diferente UNIQUE):
    Simula una excepción IntegrityError que NO es uq_processed_signal.

    Debe ser capturada por la cláusula `except IntegrityError`, pero al no coincidir
    el constraint_name, debe caer en el bloque `else`, hacer rollback y propagar la
    excepción.
    → NO XACK
    → Mensaje queda pending.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id = uuid4()

    try:
        await _create_mission(async_session, mission_id)
        await _publish_instagram_signal(redis, mission_id)

        cg = await _setup_consumer_group(redis)
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        # Simular un IntegrityError con un constraint_name diferente
        class FakeCause:
            constraint_name = "some_other_fk_constraint"

        class FakeOrig(Exception):
            __cause__ = FakeCause()

        original_add = ProcessedSignalRepository.add

        async def patched_add(self, model):  # noqa: ARG001
            raise IntegrityError("Fake statement", "Fake params", FakeOrig())

        ProcessedSignalRepository.add = patched_add

        try:
            worker = _make_worker(cg)
            
            # Al no ser uq_processed_signal, la excepción debe propagarse
            with pytest.raises(IntegrityError):
                await worker.process_one(entry_id, envelope)

            # XACK NO debe haberse ejecutado
            pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
            pending_ids = [p["message_id"] for p in pending]
            assert entry_id in pending_ids, (
                "Otro IntegrityError: XACK NO debe ejecutarse — el mensaje debe quedar pending"
            )
        finally:
            ProcessedSignalRepository.add = original_add

    finally:
        await _cleanup_all(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 5 — Mismo fingerprint, otra mission_id → procesado OK
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dedup_same_fingerprint_different_mission_processes():
    """
    Caso 5 — Mismo fingerprint, distinta mission_id:
    El constraint UNIQUE es (mission_id, source, fingerprint).
    Mismo fingerprint en otra misión → procesado como señal nueva.

    Verifica que la deduplicación NO cruza misiones.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id_a = uuid4()
    mission_id_b = uuid4()

    try:
        await _create_mission(async_session, mission_id_a)
        await _create_mission(async_session, mission_id_b)

        fingerprint = f"instagram:{_INSTAGRAM_NATIVE_ID}"

        # Pre-registrar fingerprint para misión A
        async with async_session() as session:
            repo = ProcessedSignalRepository(session)
            await repo.add(ProcessedSignal(
                mission_id=mission_id_a, source="instagram", fingerprint=fingerprint
            ))
            await session.commit()

        # Publicar señal para misión B (mismo fingerprint)
        await _publish_instagram_signal(redis, mission_id_b)

        cg = await _setup_consumer_group(redis)
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        worker = _make_worker(cg)
        canonical, transaction = await worker.process_one(entry_id, envelope)

        # Debe procesarse como señal nueva (otra mission_id)
        assert canonical is not None, (
            "Mismo fingerprint en distinta misión debe procesarse como nuevo"
        )
        assert canonical.mission_id == mission_id_b

        # ProcessedSignal debe existir para B
        async with async_session() as session:
            repo = ProcessedSignalRepository(session)
            assert await repo.exists(mission_id_b, "instagram", fingerprint)

    finally:
        await _cleanup_all(async_session, mission_id_a)
        await _cleanup_all(async_session, mission_id_b)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 6 — Mismo fingerprint, otro source → procesado OK
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dedup_same_fingerprint_different_source_processes():
    """
    Caso 6 — Mismo fingerprint lógico, distinto source:
    El constraint es (mission_id, source, fingerprint).
    Mismo fingerprint con source diferente → procesado como señal nueva.

    Verifica que la deduplicación NO cruza fuentes.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id = uuid4()

    try:
        await _create_mission(async_session, mission_id)

        # Pre-registrar un fingerprint para source "twitter"
        async with async_session() as session:
            repo = ProcessedSignalRepository(session)
            await repo.add(ProcessedSignal(
                mission_id=mission_id,
                source="twitter",
                fingerprint="twitter:some_tweet_id",
            ))
            await session.commit()

        # Publicar señal manual con fingerprint_id explícito para source "manual_input"
        await _publish_manual_signal(redis, mission_id, fingerprint_id="some_tweet_id")

        cg = await _setup_consumer_group(redis)
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        worker = _make_worker(cg)
        canonical, transaction = await worker.process_one(entry_id, envelope)

        # Debe procesarse (source="manual_input", fingerprint="manual_input:some_tweet_id")
        assert canonical is not None, (
            "Mismo fingerprint_id en distinto source debe procesarse como nuevo"
        )

        # ProcessedSignal para manual_input debe existir
        async with async_session() as session:
            repo = ProcessedSignalRepository(session)
            assert await repo.exists(mission_id, "manual_input", "manual_input:some_tweet_id")

    finally:
        await _cleanup_all(async_session, mission_id)
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 7 — FingerprintError: NO XACK, mensaje queda pending
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_dedup_fingerprint_error_no_xack():
    """
    Caso 7 — FingerprintError:
    Payload sin identidad estable → compute_fingerprint() lanza FingerprintError.
    → NO XACK → mensaje queda pending.
    → La excepción se propaga al llamador.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)
    mission_id = uuid4()

    try:
        # Publicar señal manual SIN fingerprint_id (source="manual_input")
        # ManualSensor produce source="manual_input" — sin "fingerprint_id" en payload
        sensor = ManualSensor(
            mission_id=mission_id,
            raw_payload={"body": "Signal without stable identity", "author": "Test"},
        )
        raw_signal = await sensor.detect()

        bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
        await wrap_and_publish(raw_signal, bus)

        cg = await _setup_consumer_group(redis)
        messages = await cg.read_new(count=1)
        assert len(messages) == 1
        entry_id, envelope = messages[0]

        # process_one debe lanzar FingerprintError sin hacer XACK
        worker = _make_worker(cg)
        with pytest.raises(FingerprintError):
            await worker.process_one(entry_id, envelope)

        # Mensaje debe seguir pending (no fue XACK'd)
        pending = await redis.xpending_range(_TEST_STREAM, _TEST_GROUP, "-", "+", 10)
        pending_ids = [p["message_id"] for p in pending]
        assert entry_id in pending_ids, (
            "FingerprintError → NO XACK → mensaje debe seguir pending"
        )

    finally:
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


# ---------------------------------------------------------------------------
# Test 8 — Fingerprint determinista: mismo post → mismo fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_deterministic():
    """
    Caso 8 — Fingerprint determinista (test unitario puro, sin I/O):
    El mismo post de Instagram siempre genera el mismo fingerprint,
    independientemente del momento de captura, métricas, o caption.
    """
    from uuid import uuid4

    mission_id = uuid4()

    # Mismo post, diferentes timestamps y métricas (como en llamadas sucesivas a Apify)
    payload_v1 = {
        "url_queried": _INSTAGRAM_URL,
        "items_received": 1,
        "data": {
            "id": "CTest43Dedup",
            "caption": "Caption v1",
            "likesCount": 100,  # likes diferentes
            "timestamp": "2024-01-01T10:00:00Z",
        },
    }
    payload_v2 = {
        "url_queried": _INSTAGRAM_URL,
        "items_received": 1,
        "data": {
            "id": "CTest43Dedup",  # MISMO id
            "caption": "Caption v2 — edited",
            "likesCount": 200,  # más likes
            "timestamp": "2024-06-01T12:00:00Z",  # timestamp diferente
        },
    }

    signal_v1 = RawSignalDetected(
        sensor="instagram_apify_sensor",
        source="instagram",
        mission_id=mission_id,
        raw_payload=payload_v1,
    )
    signal_v2 = RawSignalDetected(
        sensor="instagram_apify_sensor",
        source="instagram",
        mission_id=mission_id,
        raw_payload=payload_v2,
    )

    fp1 = compute_fingerprint(signal_v1)
    fp2 = compute_fingerprint(signal_v2)

    assert fp1 == fp2 == "instagram:CTest43Dedup", (
        f"Mismo post debe generar el mismo fingerprint.\n  fp1={fp1!r}\n  fp2={fp2!r}"
    )


# ---------------------------------------------------------------------------
# Test 9 — URL fallback: limpieza de query params de tracking
# ---------------------------------------------------------------------------


def test_fingerprint_url_fallback_strips_query_params():
    """
    Caso 9 — URL fallback sin ID nativo:
    Cuando raw_payload["data"] no tiene "id" ni "shortCode",
    el fallback usa "url_queried" limpiando query params de tracking.

    Dos URLs con diferentes query params del mismo post deben generar
    el mismo fingerprint.
    """
    mission_id = uuid4()

    # Payload sin ID nativo — solo URL con diferentes query params
    payload_with_tracking = {
        "url_queried": "https://www.instagram.com/p/CTest43/?hl=es&igshid=abc123",
        "items_received": 1,
        "data": {
            # Sin "id" ni "shortCode"
            "caption": "Test caption",
            "likesCount": 50,
        },
    }
    payload_without_tracking = {
        "url_queried": "https://www.instagram.com/p/CTest43/",  # misma URL, sin params
        "items_received": 1,
        "data": {
            "caption": "Test caption",
            "likesCount": 70,
        },
    }

    signal_a = RawSignalDetected(
        sensor="instagram_apify_sensor",
        source="instagram",
        mission_id=mission_id,
        raw_payload=payload_with_tracking,
    )
    signal_b = RawSignalDetected(
        sensor="instagram_apify_sensor",
        source="instagram",
        mission_id=mission_id,
        raw_payload=payload_without_tracking,
    )

    fp_a = compute_fingerprint(signal_a)
    fp_b = compute_fingerprint(signal_b)

    assert fp_a == fp_b, (
        f"URLs del mismo post con diferentes query params deben generar el mismo fingerprint.\n"
        f"  fp_a={fp_a!r}\n  fp_b={fp_b!r}"
    )
    assert fp_a == "instagram:https://www.instagram.com/p/CTest43/", (
        f"URL fallback debe limpiar query params: {fp_a!r}"
    )
