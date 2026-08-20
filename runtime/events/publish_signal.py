"""
runtime/events/publish_signal.py

Función orquestadora mínima: RawSignalDetected → EventEnvelope → EventBus.

Responsabilidad única:
    Empaquetar un RawSignalDetected en un EventEnvelope y publicarlo en el
    Event Bus. Esta función es el único punto donde el sensor y el bus se
    conectan — el sensor nunca conoce a Redis.

Convenciones de trazabilidad (evento raíz):
    event_type    = "signal.raw.detected"
    correlation_id = event_id generado (el evento es raíz de su propio flujo)
    causation_id   = None (no fue causado por otro evento)

Por qué correlation_id == event_id:
    En un evento raíz no existe correlación previa. Establecer
    correlation_id = event_id permite que todos los eventos derivados de
    este flujo (normalización, análisis, etc.) puedan agruparse bajo el
    mismo ID sin necesidad de un campo adicional.
"""

from runtime.contracts.event_envelope import EventEnvelope
from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.events.bus import RedisEventBus

EVENT_TYPE: str = "signal.raw.detected"


async def wrap_and_publish(
    signal: RawSignalDetected,
    bus: RedisEventBus,
) -> EventEnvelope:
    """
    Empaqueta un RawSignalDetected en un EventEnvelope y lo publica en el bus.

    El EventEnvelope genera su event_id automáticamente. Tras la creación,
    correlation_id se fija igual al event_id para identificar este flujo
    como su propio origen.

    Args:
        signal: RawSignalDetected producido por un BaseSensor.
        bus:    RedisEventBus activo. El llamador gestiona su ciclo de vida.

    Returns:
        EventEnvelope: el envelope publicado (con event_id, correlation_id, etc.)

    Raises:
        redis.RedisError: Si la escritura en el stream falla.
    """
    envelope = EventEnvelope(
        event_type=EVENT_TYPE,
        payload=signal.model_dump(mode="json"),
        causation_id=None,
        # correlation_id se ajusta post-creación para usar el event_id generado
    )
    # Convención raíz: correlation_id = event_id propio
    envelope = envelope.model_copy(update={"correlation_id": envelope.event_id})

    await bus.publish(envelope)
    return envelope
