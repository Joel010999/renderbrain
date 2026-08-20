"""
runtime/engines/sensors/manual.py

ManualSensor — Sensor de entrada controlada para el First Signal Flow.

Responsabilidad única:
    Recibir un payload arbitrario inyectado por el llamador y empaquetarlo
    como un RawSignalDetected válido. No normaliza, no persiste, no toca Redis.

Uso típico (en un orquestador o test):
    sensor = ManualSensor(
        mission_id=some_uuid,
        raw_payload={"title": "...", "body": "..."},
    )
    signal: RawSignalDetected = await sensor.detect()

Constantes del sensor:
    SENSOR_NAME = "manual"
    SOURCE_NAME = "manual_input"

    Estos valores identifican inequívocamente el origen de la señal en el
    EventEnvelope y en futuros índices de trazabilidad.
"""

from uuid import UUID

from runtime.contracts.interfaces import BaseSensor
from runtime.contracts.raw_signal_detected import RawSignalDetected

SENSOR_NAME: str = "manual"
SOURCE_NAME: str = "manual_input"


class ManualSensor(BaseSensor):
    """
    Sensor de entrada manual/controlada.

    Implementa BaseSensor.detect() → RawSignalDetected.

    Recibe el payload en el constructor y lo devuelve empaquetado.
    Esto garantiza que el sensor es stateless respecto a Redis y que
    cualquier orquestador puede inyectar datos de prueba sin efectos.

    Args:
        mission_id:  UUID de la misión a la que pertenece esta captura.
        raw_payload: Datos crudos JSON-serializables a capturar.
    """

    def __init__(self, mission_id: UUID, raw_payload: dict) -> None:
        self._mission_id = mission_id
        self._raw_payload = raw_payload

    async def detect(self) -> RawSignalDetected:
        """
        Produce un RawSignalDetected desde la entrada manual inyectada.

        No realiza I/O ni efectos secundarios. El campo captured_at se
        autogenera con datetime.now(UTC) en la instanciación del contrato.

        Returns:
            RawSignalDetected: señal lista para ser empaquetada en EventEnvelope.
        """
        return RawSignalDetected(
            sensor=SENSOR_NAME,
            source=SOURCE_NAME,
            mission_id=self._mission_id,
            raw_payload=self._raw_payload,
        )
