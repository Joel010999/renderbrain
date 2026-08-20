"""
runtime/orchestration/mission_scheduler.py

Orquestador para la ejecución periódica de Missions.
"""

import logging

from runtime.contracts.event_envelope import EventEnvelope
from runtime.contracts.mission import Mission
from runtime.engines.sensors.factory import SensorFactory
from runtime.engines.sensors.instagram import InstagramSensorError
from runtime.events.bus import RedisEventBus
from runtime.events.publish_signal import wrap_and_publish
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


class MissionExecutionError(Exception):
    """Error interno al ejecutar la misión, no se detiene el proceso."""


class MissionSchedulerOrchestrator:
    """
    Coordina la ejecución aislada de una Mission.
    
    Responsabilidades:
    - Validar que la misión esté habilitada.
    - Resolver el sensor vía SensorFactory.
    - Capturar la señal.
    - Publicar en Redis de forma segura.
    """

    def __init__(self, sensor_factory: SensorFactory, event_bus: RedisEventBus) -> None:
        self._sensor_factory = sensor_factory
        self._event_bus = event_bus

    async def execute_mission(self, mission: Mission) -> EventEnvelope | None:
        """
        Ejecuta la misión y publica la señal capturada.

        Fallo seguro: Los errores de red, permisos del sensor o fallos
        de Redis son capturados y logueados sin propagarse, para no
        detener al APScheduler. No hay retries a este nivel.

        Returns:
            EventEnvelope publicado si fue exitoso.
            None si hubo un error o la misión está deshabilitada.
        """
        if not mission.enabled:
            logger.info(
                "Mission execution skipped (disabled)",
                extra={"mission_id": str(mission.id)},
            )
            return None

        logger.info(
            "Executing mission",
            extra={
                "mission_id": str(mission.id),
                "source": mission.source,
                "target": mission.target,
            },
        )

        try:
            # 1. Resolver el sensor
            sensor = self._sensor_factory.build_sensor(mission)

            # 2. Capturar señal
            raw_signal = await sensor.detect()

            # Garantizar que el sensor usó el mission_id correcto
            if raw_signal.mission_id != mission.id:
                logger.error(
                    "Sensor produced a signal with mismatched mission_id",
                    extra={
                        "expected": str(mission.id),
                        "actual": str(raw_signal.mission_id),
                    },
                )
                return None

            # 3. Publicar
            envelope = await wrap_and_publish(raw_signal, self._event_bus)

            logger.info(
                "Mission executed and signal published",
                extra={
                    "mission_id": str(mission.id),
                    "event_id": str(envelope.event_id),
                },
            )
            return envelope

        except InstagramSensorError as e:
            # Errores esperados del sensor de Instagram (red, sin datos, etc)
            logger.warning(
                "Sensor failed to detect signal",
                extra={
                    "mission_id": str(mission.id),
                    "source": mission.source,
                    "target": mission.target,
                    "error": str(e),
                },
                exc_info=True,
            )
            return None
            
        except Exception as e:
            # Errores inesperados, incluyendo fallos al publicar en Redis
            logger.error(
                "Mission execution failed unexpectedly",
                extra={
                    "mission_id": str(mission.id),
                    "source": mission.source,
                    "target": mission.target,
                    "error": str(e),
                },
                exc_info=True,
            )
            return None
