"""
Test de integración — Event Bus completo: Producer → Redis Stream → Consumer.

Requisito previo: contenedor renderbrain-redis corriendo y healthy.

    docker compose up -d
    uv run pytest tests/integration/test_event_bus.py -v -m integration

Ciclo verificado:
    1. Se instancia un EventEnvelope con campos explícitos.
    2. Se publica en el Event Bus (XADD al stream de prueba aislado).
    3. Se lee de vuelta (XRANGE desde el inicio).
    4. Se hace assertions estrictos sobre TODOS los campos del envelope.
    5. Se limpia el stream de prueba al finalizar.

Separación de IDs verificada:
    El Entry ID retornado por publish() es diferente al event_id del envelope.
    El read() reconstruye el envelope con el event_id original intacto.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from runtime.contracts import EventEnvelope
from runtime.events import RedisEventBus
from runtime.infrastructure.redis.client import get_redis_client

# Stream aislado para los tests — nunca colisiona con streams de producción
_TEST_STREAM = "renderbrain:test:events:c3.2"


@pytest.mark.integration
async def test_event_bus_roundtrip_full_fields():
    """
    Ciclo completo Producer → Stream → Consumer con todos los campos explícitos.

    Verifica que EventEnvelope se serializa y deserializa correctamente
    manteniendo la identidad exacta de todos sus campos:
    - event_id, event_type, occurred_at, schema_version
    - payload (estructura anidada)
    - correlation_id, causation_id (presentes)

    También verifica que el Redis Entry ID es distinto al event_id de RenderBrain.
    """
    redis = get_redis_client()

    # Eliminar el stream antes del test para garantizar estado limpio
    await redis.delete(_TEST_STREAM)

    try:
        # ----------------------------------------------------------------
        # 1. Construir envelope con todos los campos explícitos
        # ----------------------------------------------------------------
        fixed_event_id = uuid4()
        fixed_correlation_id = uuid4()
        fixed_causation_id = uuid4()
        fixed_occurred_at = datetime.now(UTC)

        original = EventEnvelope(
            event_id=fixed_event_id,
            event_type="test.event.full",
            occurred_at=fixed_occurred_at,
            payload={
                "source": "c3.2-integration-test",
                "value": 42,
                "nested": {"key": "value"},
            },
            schema_version="1.0",
            correlation_id=fixed_correlation_id,
            causation_id=fixed_causation_id,
        )

        # ----------------------------------------------------------------
        # 2. Publicar en el Event Bus
        # ----------------------------------------------------------------
        bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
        redis_entry_id: str = await bus.publish(original)

        # El Entry ID de Redis es una string tipo "1753315200000-0"
        # Debe ser diferente al event_id del envelope (UUID)
        assert redis_entry_id != str(original.event_id), (
            "El Redis Entry ID no debe ser igual al event_id del envelope"
        )

        # ----------------------------------------------------------------
        # 3. Leer de vuelta desde el inicio del stream
        # ----------------------------------------------------------------
        events = await bus.read(count=1, last_id="0-0")

        assert len(events) == 1, (
            f"Se esperaba 1 evento en el stream, se obtuvieron: {len(events)}"
        )

        reconstructed = events[0]

        # ----------------------------------------------------------------
        # 4. Assertions estrictos — campo por campo
        # ----------------------------------------------------------------
        assert reconstructed.event_id == original.event_id, (
            f"event_id difiere: {reconstructed.event_id!r} != {original.event_id!r}"
        )
        assert reconstructed.event_type == original.event_type, (
            f"event_type difiere: {reconstructed.event_type!r}"
        )
        assert reconstructed.schema_version == original.schema_version, (
            f"schema_version difiere: {reconstructed.schema_version!r}"
        )
        assert reconstructed.payload == original.payload, (
            f"payload difiere: {reconstructed.payload!r}"
        )
        assert reconstructed.correlation_id == original.correlation_id, (
            f"correlation_id difiere: {reconstructed.correlation_id!r}"
        )
        assert reconstructed.causation_id == original.causation_id, (
            f"causation_id difiere: {reconstructed.causation_id!r}"
        )
        # occurred_at: verificar que se preserva con timezone UTC
        assert reconstructed.occurred_at == original.occurred_at, (
            f"occurred_at difiere: {reconstructed.occurred_at!r} != {original.occurred_at!r}"
        )
        assert reconstructed.occurred_at.tzinfo is not None, (
            "occurred_at debe ser timezone-aware"
        )

    finally:
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


@pytest.mark.integration
async def test_event_bus_root_event_no_correlation():
    """
    Evento raíz: correlation_id y causation_id son None.

    Verifica que:
    - Se puede crear un EventEnvelope sin correlation_id ni causation_id.
    - Los valores None se serializan a null en JSON y se reconstruyen como None.
    - El ciclo publish → read preserva correctamente los campos opcionales.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    try:
        # Evento raíz: no tiene evento causante ni correlación previa
        original = EventEnvelope(
            event_type="test.event.root",
            payload={"description": "evento raíz sin correlación"},
            # correlation_id y causation_id toman default None
        )

        assert original.correlation_id is None
        assert original.causation_id is None

        bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
        await bus.publish(original)

        events = await bus.read(count=1, last_id="0-0")
        assert len(events) == 1

        reconstructed = events[0]

        assert reconstructed.event_id == original.event_id
        assert reconstructed.event_type == "test.event.root"
        assert reconstructed.correlation_id is None, (
            f"correlation_id debe ser None, obtenido: {reconstructed.correlation_id!r}"
        )
        assert reconstructed.causation_id is None, (
            f"causation_id debe ser None, obtenido: {reconstructed.causation_id!r}"
        )
        assert reconstructed.payload == original.payload

    finally:
        await redis.delete(_TEST_STREAM)
        await redis.aclose()


@pytest.mark.integration
async def test_event_bus_multiple_events_ordered():
    """
    Publica múltiples eventos y verifica que read() los retorna en orden FIFO.

    Redis Streams garantiza orden de inserción — esta prueba lo confirma
    explícitamente para la implementación actual.
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    try:
        bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)

        types = ["test.event.first", "test.event.second", "test.event.third"]
        published_ids: list[UUID] = []

        for event_type in types:
            envelope = EventEnvelope(
                event_type=event_type,
                payload={"order": types.index(event_type)},
            )
            published_ids.append(envelope.event_id)
            await bus.publish(envelope)

        events = await bus.read(count=10, last_id="0-0")

        assert len(events) == 3, f"Se esperaban 3 eventos, obtenidos: {len(events)}"

        for i, event in enumerate(events):
            assert event.event_id == published_ids[i], (
                f"Posición {i}: event_id {event.event_id!r} != {published_ids[i]!r}"
            )
            assert event.event_type == types[i], (
                f"Posición {i}: event_type {event.event_type!r} != {types[i]!r}"
            )

    finally:
        await redis.delete(_TEST_STREAM)
        await redis.aclose()
