"""
Test de integración S1.2 — First Signal Flow completo.

Flujo verificado:
    ManualSensor.detect()           →  RawSignalDetected
    wrap_and_publish(signal, bus)   →  EventEnvelope (publicado en Redis Stream)
    bus.read()                      →  EventEnvelope reconstruido

Requisito previo: contenedor renderbrain-redis corriendo y healthy.

    docker compose up -d
    uv run pytest tests/integration/test_manual_sensor_flow.py -v -m integration

Convenciones de trazabilidad verificadas:
    event_type     == "signal.raw.detected"
    correlation_id == event_id  (evento raíz)
    causation_id   is None
    payload        == RawSignalDetected serializado (reconstructible)
"""

from uuid import UUID, uuid4

import pytest

from runtime.contracts import EventEnvelope, RawSignalDetected
from runtime.engines.sensors import ManualSensor
from runtime.events import EVENT_TYPE, RedisEventBus, wrap_and_publish
from runtime.infrastructure.redis.client import get_redis_client

# Stream aislado para S1.2 — nunca colisiona con otros streams
_TEST_STREAM = "renderbrain:test:events:s1.2"


@pytest.mark.integration
async def test_manual_sensor_flow_full():
    """
    Ciclo completo: ManualSensor → RawSignalDetected → EventEnvelope → Redis → leer.

    Aserciones obligatorias (criterios de salida S1.2):
      ✓ event_type == "signal.raw.detected"
      ✓ event_id existe (no None)
      ✓ correlation_id == event_id  (convención evento raíz)
      ✓ causation_id is None
      ✓ payload reconstruible como RawSignalDetected con campos intactos:
            mission_id, sensor, source, captured_at, raw_payload
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    try:
        # ----------------------------------------------------------------
        # 1. Input controlado → ManualSensor.detect()
        # ----------------------------------------------------------------
        mission_id: UUID = uuid4()
        raw_payload = {
            "title": "RenderBrain S1.2 — First Signal",
            "body": "Señal de prueba inyectada manualmente.",
            "tags": ["renderbrain", "sprint1", "manual"],
        }

        sensor = ManualSensor(mission_id=mission_id, raw_payload=raw_payload)
        signal: RawSignalDetected = await sensor.detect()

        # Validar que el sensor produjo un contrato correcto
        assert signal.sensor == "manual"
        assert signal.source == "manual_input"
        assert signal.mission_id == mission_id
        assert signal.raw_payload == raw_payload
        assert signal.captured_at is not None

        # ----------------------------------------------------------------
        # 2. Empaquetar y publicar: wrap_and_publish → Redis Stream
        # ----------------------------------------------------------------
        bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
        published: EventEnvelope = await wrap_and_publish(signal, bus)

        # ----------------------------------------------------------------
        # 3. Leer de vuelta desde el stream
        # ----------------------------------------------------------------
        events = await bus.read(count=1, last_id="0-0")
        assert len(events) == 1, (
            f"Se esperaba 1 evento en el stream, obtenidos: {len(events)}"
        )
        reconstructed = events[0]

        # ----------------------------------------------------------------
        # 4. Aserciones de trazabilidad
        # ----------------------------------------------------------------
        assert reconstructed.event_type == EVENT_TYPE, (
            f"event_type esperado '{EVENT_TYPE}', obtenido: {reconstructed.event_type!r}"
        )
        assert reconstructed.event_id is not None, (
            "event_id debe existir y no ser None"
        )
        assert reconstructed.correlation_id == reconstructed.event_id, (
            f"correlation_id debe ser igual a event_id en un evento raíz. "
            f"event_id={reconstructed.event_id!r}, "
            f"correlation_id={reconstructed.correlation_id!r}"
        )
        assert reconstructed.causation_id is None, (
            f"causation_id debe ser None en un evento raíz, "
            f"obtenido: {reconstructed.causation_id!r}"
        )

        # Verificar que published y reconstructed son el mismo evento
        assert reconstructed.event_id == published.event_id, (
            "event_id publicado y leído difieren"
        )

        # ----------------------------------------------------------------
        # 5. Reconstruir RawSignalDetected desde el payload
        # ----------------------------------------------------------------
        reconstructed_signal = RawSignalDetected.model_validate(
            reconstructed.payload
        )

        assert reconstructed_signal.mission_id == signal.mission_id, (
            f"mission_id difiere: {reconstructed_signal.mission_id!r}"
        )
        assert reconstructed_signal.sensor == signal.sensor, (
            f"sensor difiere: {reconstructed_signal.sensor!r}"
        )
        assert reconstructed_signal.source == signal.source, (
            f"source difiere: {reconstructed_signal.source!r}"
        )
        assert reconstructed_signal.captured_at == signal.captured_at, (
            f"captured_at difiere: {reconstructed_signal.captured_at!r}"
        )
        assert reconstructed_signal.raw_payload == signal.raw_payload, (
            f"raw_payload difiere: {reconstructed_signal.raw_payload!r}"
        )

    finally:
        await redis.delete(_TEST_STREAM)
        await redis.aclose()

