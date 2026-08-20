"""
runtime/engines/sensors/instagram.py

InstagramSensor — Sensor de captura de publicaciones públicas de Instagram.

Responsabilidad única:
    Recibir una URL pública de Instagram, delegar la obtención de datos al
    ApifyInstagramAdapter (inyectado), y empaquetar el resultado crudo como
    un RawSignalDetected válido.

Decisiones de diseño:
    - El adaptador se inyecta en el constructor (DI): el sensor no conoce
      detalles de Apify, no importa apify-client, no instancia infraestructura.
    - `source` es siempre "instagram": representa la fuente de dominio real,
      no el proveedor técnico (Apify es un detalle de infraestructura).
    - `raw_payload` conserva íntegramente el primer ítem devuelto por Apify
      más metadatos de trazabilidad (url_queried, items_received) para
      garantizar que no se pierde ningún dato crudo.
    - limit=1 por defecto: el sensor captura la señal mínima; si se necesitan
      más ítems en el futuro se parametriza explícitamente.

Constantes del sensor:
    SENSOR_NAME = "instagram_apify_sensor"
    SOURCE_NAME = "instagram"
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from runtime.contracts.interfaces import BaseSensor
from runtime.contracts.raw_signal_detected import RawSignalDetected

SENSOR_NAME: str = "instagram_apify_sensor"
SOURCE_NAME: str = "instagram"


class PostFetcher(Protocol):
    """Interfaz estructural mínima requerida por InstagramSensor.

    Cualquier objeto que implemente este método satisface el contrato
    sin necesidad de herencia explícita (structural subtyping).
    ApifyInstagramAdapter y los FakeAdapter de tests lo cumplen.
    """

    def fetch_post(self, url: str, limit: int = 1) -> list[dict[str, Any]]:
        ...


class InstagramSensorError(Exception):
    """Error base del dominio para InstagramSensor.

    Se lanza cuando el sensor no puede producir un RawSignalDetected válido.
    Nunca debe contener secretos de configuración en su mensaje.
    """


class InstagramSensorEmptyResultError(InstagramSensorError):
    """El adaptador devolvió una lista vacía o sin datos utilizables."""


class InstagramSensorAdapterError(InstagramSensorError):
    """El adaptador lanzó una excepción que impidió la captura."""


class InstagramSensor(BaseSensor):
    """Sensor de dominio que captura señales desde Instagram vía Apify.

    Implementa BaseSensor.detect() → RawSignalDetected.

    No conoce detalles internos del SDK de Apify ni de ningún proveedor.
    La única dependencia de infraestructura es el adaptador inyectado, al que
    se accede exclusivamente a través de su interfaz pública (fetch_post).

    Args:
        mission_id:  UUID de la misión a la que pertenece esta captura.
        url:         URL pública de Instagram (post o reel).
        adapter:     Instancia de ApifyInstagramAdapter u otro objeto que
                     implemente el método fetch_post(url, limit) → list[dict].
                     Se inyecta desde fuera para mantener el sensor testeable
                     sin llamar a Apify.

    Example::

        from runtime.infrastructure.apify import ApifyInstagramAdapter
        from runtime.engines.sensors.instagram import InstagramSensor

        sensor = InstagramSensor(
            mission_id=some_uuid,
            url="https://www.instagram.com/p/ABC123/",
            adapter=ApifyInstagramAdapter(),
        )
        signal: RawSignalDetected = await sensor.detect()
    """

    def __init__(
        self,
        mission_id: UUID,
        url: str,
        adapter: PostFetcher,
    ) -> None:
        self._mission_id = mission_id
        self._url = url
        self._adapter = adapter

    async def detect(self) -> RawSignalDetected:
        """Captura una señal de Instagram y la empaqueta como RawSignalDetected.

        Flujo:
            1. Llama a adapter.fetch_post(url, limit=1).
            2. Valida que el resultado no esté vacío.
            3. Construye raw_payload con los datos crudos + metadatos de trazabilidad.
            4. Retorna RawSignalDetected con source="instagram".

        Returns:
            RawSignalDetected: señal lista para ser empaquetada en EventEnvelope.

        Raises:
            InstagramSensorEmptyResultError: el adapter devolvió lista vacía.
            InstagramSensorAdapterError:     el adapter lanzó una excepción.
        """
        try:
            items: list[dict] = self._adapter.fetch_post(self._url, limit=1)
        except Exception as exc:
            # Propagamos como error de dominio; no re-lanzamos exc directamente
            # para evitar que mensajes del SDK expongan detalles de infraestructura.
            raise InstagramSensorAdapterError(
                f"El adaptador falló al obtener datos de '{self._url}': "
                f"{type(exc).__name__}"
            ) from exc

        if not items:
            raise InstagramSensorEmptyResultError(
                f"El adaptador devolvió una lista vacía para '{self._url}'. "
                "Verificá que la URL sea pública y accesible."
            )

        # raw_payload conserva íntegramente el primer ítem crudo de Apify
        # más metadatos de trazabilidad para auditoría futura.
        raw_payload: dict = {
            "url_queried": self._url,
            "items_received": len(items),
            "data": items[0],  # ítem crudo sin transformación
        }

        return RawSignalDetected(
            sensor=SENSOR_NAME,
            source=SOURCE_NAME,
            mission_id=self._mission_id,
            raw_payload=raw_payload,
        )
