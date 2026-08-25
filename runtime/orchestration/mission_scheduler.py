"""
runtime/orchestration/mission_scheduler.py

Orquestador para la ejecución periódica de Missions.

A1.1 — Extensión:
    - execute_mission() detecta mission.target_type y delega al orquestador correcto:
        "post"    → lógica existente (InstagramSensor + señal individual)
        "profile" → ProfileCollectionOrchestrator (Posts + Reels multi-señal)
    - Retrocompatibilidad total: misiones existentes con target_type=post siguen
      el mismo flujo sin ningún cambio de comportamiento.
"""

import logging

from runtime.contracts.event_envelope import EventEnvelope
from runtime.contracts.mission import Mission
from runtime.engines.sensors.factory import DefaultSensorFactory, SensorFactory
from runtime.engines.sensors.instagram import InstagramSensorError
from runtime.events.bus import RedisEventBus
from runtime.events.publish_signal import wrap_and_publish
from runtime.orchestration.profile_collection_orchestrator import ProfileCollectionOrchestrator
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


class MissionExecutionError(Exception):
    """Error interno al ejecutar la misión, no se detiene el proceso."""


class MissionSchedulerOrchestrator:
    """
    Coordina la ejecución aislada de una Mission.

    Responsabilidades:
    - Validar que la misión esté habilitada.
    - Delegar al sensor/orquestador correcto según target_type.
    - Capturar la señal y publicar en Redis de forma segura.
    """

    def __init__(self, sensor_factory: SensorFactory, event_bus: RedisEventBus) -> None:
        self._sensor_factory = sensor_factory
        self._event_bus = event_bus
        # Orquestador de perfiles (A1.1) — reutiliza el mismo sensor_factory y bus
        self._profile_orchestrator = ProfileCollectionOrchestrator(
            sensor_factory=sensor_factory,
            event_bus=event_bus,
        )

    async def execute_mission(self, mission: Mission) -> EventEnvelope | None:
        """
        Ejecuta la misión y publica la(s) señal(es) capturada(s).

        Para target_type='post': publica un único EventEnvelope (legado S2.1).
        Para target_type='profile': delega a ProfileCollectionOrchestrator
            que publica un EventEnvelope por cada ítem (Posts + Reels).

        Fallo seguro: Los errores son capturados y logueados sin propagarse,
        para no detener al APScheduler.

        Returns:
            Para post: EventEnvelope publicado, o None si hubo error.
            Para profile: El primer EventEnvelope de la lista, o None.
              (El retorno es Optional para mantener compatibilidad de tipo).
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
                "target_type": mission.target_type,
            },
        )

        # ------------------------------------------------------------------
        # DELEGACIÓN POR TARGET_TYPE
        # ------------------------------------------------------------------
        if mission.target_type == "profile":
            return await self._execute_profile(mission)

        # Default: target_type == "post" (legado S2.1 — sin cambios)
        return await self._execute_post(mission)

    async def execute_stories(self, mission: Mission) -> list[EventEnvelope]:
        """
        Ejecuta SOLO la recolección de Stories de una misión de perfil.

        Llamado desde el job de stories del Scheduler (cada story_interval_seconds).
        El fallo es soft — retorna [] si stories no están disponibles.

        Args:
            mission: Mission con target_type='profile'.

        Returns:
            Lista de EventEnvelopes de stories. Vacía si falló o no hay stories.
        """
        if not mission.enabled:
            logger.info(
                "Stories mission skipped (disabled)",
                extra={"mission_id": str(mission.id)},
            )
            return []

        if mission.target_type != "profile":
            logger.warning(
                "execute_stories() called on non-profile mission — skipping",
                extra={"mission_id": str(mission.id), "target_type": mission.target_type},
            )
            return []

        return await self._profile_orchestrator.execute_profile_stories_mission(mission)

    # ------------------------------------------------------------------
    # Implementaciones privadas
    # ------------------------------------------------------------------

    async def _execute_post(self, mission: Mission) -> EventEnvelope | None:
        """Ejecución legacy para target_type='post' — sin cambios respecto a S2.1."""
        try:
            sensor = self._sensor_factory.build_sensor(mission)
            raw_signal = await sensor.detect()

            if raw_signal.mission_id != mission.id:
                logger.error(
                    "Sensor produced a signal with mismatched mission_id",
                    extra={
                        "expected": str(mission.id),
                        "actual": str(raw_signal.mission_id),
                    },
                )
                return None

            envelope = await wrap_and_publish(raw_signal, self._event_bus)
            logger.info(
                "Post mission executed and signal published",
                extra={
                    "mission_id": str(mission.id),
                    "event_id": str(envelope.event_id),
                },
            )
            return envelope

        except InstagramSensorError as e:
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

    async def _execute_profile(self, mission: Mission) -> EventEnvelope | None:
        """Ejecución para target_type='profile' — A1.1."""
        try:
            envelopes = await self._profile_orchestrator.execute_profile_mission(mission)
            if envelopes:
                logger.info(
                    "Profile mission executed — signals published",
                    extra={
                        "mission_id": str(mission.id),
                        "username": mission.target,
                        "count": len(envelopes),
                    },
                )
                return envelopes[0]  # primer envelope para compatibilidad de tipo
            return None
        except Exception as e:
            logger.error(
                "Profile mission execution failed unexpectedly",
                extra={
                    "mission_id": str(mission.id),
                    "username": mission.target,
                    "error": str(e),
                },
                exc_info=True,
            )
            return None
