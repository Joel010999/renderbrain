"""
tests/e2e/test_first_signal_flow.py

Test E2E S1.4 — First Signal Flow completo.

Flujo verificado (sin mocks de infraestructura):
    Input controlado
        → ManualSensor.detect()           → RawSignalDetected
        → wrap_and_publish(signal, bus)   → EventEnvelope (publicado en Redis)
        → bus.read()                      → EventEnvelope reconstruido
        → run_signal_flow(envelope, session)
            → RawSignalDetected (reconstruido)
            → NormalizerEngine.normalize()  → CanonicalSignal base
            → model_copy(source_event_id=envelope.event_id) → trazabilidad real
            → CanonicalSignalRepository.save() → PostgreSQL
        → repository.get_by_id()          → CanonicalSignal leído de DB

Requisito previo: contenedores renderbrain-postgres y renderbrain-redis corriendo.

    docker compose up -d
    uv run pytest tests/e2e/test_first_signal_flow.py -v -m integration

Casos verificados:
    1. Flujo completo: publicación → consumo → normalización → persistencia → lectura.
    2. Trazabilidad: source_event_id == envelope.event_id.
    3. mission_id idéntico en toda la cadena.
    4. Campos normalizados correctos: source, sensor, content, author, metrics.
    5. captured_at heredado del RawSignalDetected (timezone-aware en UTC).
    6. normalized_at autogenerado y timezone-aware en UTC.
    7. El objeto leído de DB es CanonicalSignal (Pydantic), no un ORM model.
    8. RawSignalDetected y CanonicalSignal son contratos separados e independientes.

Limpieza: DELETE explícito en PostgreSQL + eliminación del stream en Redis.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete

from runtime.contracts import CanonicalSignal, RawSignalDetected
from runtime.contracts.event_envelope import EventEnvelope
from runtime.engines.sensors import ManualSensor
from runtime.events import EVENT_TYPE, RedisEventBus, wrap_and_publish
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.repositories import CanonicalSignalRepository
from runtime.infrastructure.redis.client import get_redis_client
from runtime.orchestration.signal_flow import run_signal_flow

# Stream aislado — nunca colisiona con producción ni con otros tests
_TEST_STREAM = "renderbrain:test:events:s1.4:e2e"


@pytest.mark.integration
async def test_first_signal_flow_end_to_end():
    """
    Flujo E2E completo: Input manual → Redis → NormalizerEngine → PostgreSQL.

    Verificaciones obligatorias (criterios de salida S1.4):
      ✓ id: UUID válido
      ✓ mission_id idéntico en toda la cadena
      ✓ source_event_id == envelope.event_id (trazabilidad completa)
      ✓ source == "manual_input" (heredado de ManualSensor)
      ✓ sensor == "manual" (heredado de ManualSensor)
      ✓ content normalizado desde raw_payload["body"]
      ✓ author normalizado desde raw_payload["author"]
      ✓ metrics normalizados desde raw_payload["metrics"]
      ✓ captured_at heredado y timezone-aware en UTC
      ✓ normalized_at generado y timezone-aware en UTC
      ✓ El objeto recuperado de DB es CanonicalSignal (Pydantic), no ORM
      ✓ RawSignalDetected y CanonicalSignal son instancias distintas
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    canonical_id: UUID | None = None

    try:
        # ------------------------------------------------------------------
        # 1. Input controlado — payload representativo con todos los campos
        # ------------------------------------------------------------------
        mission_id: UUID = uuid4()
        raw_payload = {
            "body": "RenderBrain Sprint 1 — First Signal Flow E2E test.",
            "author": "RenderBrain TestSuite",
            "language": "en",
            "metrics": {"likes": 7, "reach": 500, "score": 0.88},
        }

        # ------------------------------------------------------------------
        # 2. ManualSensor → RawSignalDetected
        # ------------------------------------------------------------------
        sensor = ManualSensor(mission_id=mission_id, raw_payload=raw_payload)
        raw_signal: RawSignalDetected = await sensor.detect()

        assert raw_signal.sensor == "manual"
        assert raw_signal.source == "manual_input"
        assert raw_signal.mission_id == mission_id
        assert raw_signal.captured_at is not None
        assert raw_signal.captured_at.tzinfo is not None  # timezone-aware

        # ------------------------------------------------------------------
        # 3. wrap_and_publish → Redis Stream
        # ------------------------------------------------------------------
        bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
        published: EventEnvelope = await wrap_and_publish(raw_signal, bus)

        assert published.event_type == EVENT_TYPE
        assert published.event_id is not None
        assert published.correlation_id == published.event_id

        # ------------------------------------------------------------------
        # 4. Leer de vuelta desde Redis Stream
        # ------------------------------------------------------------------
        events = await bus.read(count=1, last_id="0-0")
        assert len(events) == 1, (
            f"Se esperaba 1 evento en el stream, obtenidos: {len(events)}"
        )
        envelope: EventEnvelope = events[0]
        assert envelope.event_id == published.event_id

        # ------------------------------------------------------------------
        # 5. Orquestación: run_signal_flow → normalizar → persistir
        # ------------------------------------------------------------------
        async with async_session() as session:
            try:
                canonical: CanonicalSignal = await run_signal_flow(
                    envelope=envelope,
                    session=session,
                )
                await session.commit()
                canonical_id = canonical.id

                # ----------------------------------------------------------
                # 6. Leer desde PostgreSQL vía repository
                # ----------------------------------------------------------
                repo = CanonicalSignalRepository(session)
                recovered: CanonicalSignal | None = await repo.get_by_id(canonical_id)

                # 6a. El objeto recuperado es un CanonicalSignal (Pydantic), no ORM
                assert recovered is not None, (
                    "get_by_id() no debe devolver None para una señal recién persistida"
                )
                assert isinstance(recovered, CanonicalSignal), (
                    f"Se esperaba CanonicalSignal, obtenido: {type(recovered)}"
                )

                # 6b. RawSignalDetected y CanonicalSignal son contratos separados
                assert not isinstance(recovered, RawSignalDetected), (
                    "CanonicalSignal no debe ser una instancia de RawSignalDetected"
                )

                # 6c. Identidad
                assert isinstance(recovered.id, UUID), "id debe ser un UUID válido"
                assert recovered.id == canonical.id, "id debe coincidir tras persistencia"

                # 6d. mission_id idéntico en toda la cadena
                assert recovered.mission_id == mission_id, (
                    f"mission_id difiere: original={mission_id}, "
                    f"recovered={recovered.mission_id}"
                )

                # 6e. Trazabilidad: source_event_id == envelope.event_id
                assert recovered.source_event_id == envelope.event_id, (
                    f"source_event_id debe ser el event_id del envelope.\n"
                    f"  envelope.event_id  = {envelope.event_id}\n"
                    f"  recovered.source_event_id = {recovered.source_event_id}"
                )

                # 6f. Campos de origen (heredados del sensor)
                assert recovered.source == "manual_input", (
                    f"source difiere: {recovered.source!r}"
                )
                assert recovered.sensor == "manual", (
                    f"sensor difiere: {recovered.sensor!r}"
                )

                # 6g. Contenido normalizado desde raw_payload["body"]
                assert recovered.content == raw_payload["body"], (
                    f"content difiere: {recovered.content!r}"
                )

                # 6h. Autor normalizado
                assert recovered.author == raw_payload["author"], (
                    f"author difiere: {recovered.author!r}"
                )

                # 6i. Métricas normalizadas
                assert recovered.metrics == raw_payload["metrics"], (
                    f"metrics difiere: {recovered.metrics!r}"
                )

                # 6j. captured_at heredado y timezone-aware
                assert recovered.captured_at == raw_signal.captured_at, (
                    f"captured_at difiere: {recovered.captured_at!r}"
                )
                assert recovered.captured_at.tzinfo is not None, (
                    "captured_at debe ser timezone-aware"
                )

                # 6k. normalized_at autogenerado y timezone-aware en UTC
                assert recovered.normalized_at is not None, (
                    "normalized_at no debe ser None"
                )
                assert recovered.normalized_at.tzinfo is not None, (
                    "normalized_at debe ser timezone-aware"
                )

            finally:
                # Limpiar la fila en PostgreSQL — DELETE explícito, sin TRUNCATE
                await session.rollback()
                if canonical_id is not None:
                    await session.execute(
                        delete(CanonicalSignalModel).where(
                            CanonicalSignalModel.id == canonical_id
                        )
                    )
                    await session.commit()

    finally:
        # Limpiar el stream de Redis — DELETE del stream aislado
        await redis.delete(_TEST_STREAM)
        await redis.aclose()
