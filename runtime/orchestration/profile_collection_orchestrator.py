"""
runtime/orchestration/profile_collection_orchestrator.py

ProfileCollectionOrchestrator — A1.1

Responsabilidad:
    Orquestar la recolección de contenido de perfiles de Instagram.
    Cada ítem (Post, Reel o Story) se publica como EventEnvelope independiente
    bajo el mismo mission_id, acumulando señales para el Agente 2.

Estrategia event-per-item:
    1. Instanciar InstagramProfileSensor vía SensorFactory.
    2. detect() → list[RawSignalDetected] (Posts + Reels).
    3. Por cada RawSignalDetected: wrap_and_publish() → EventEnvelope independiente.
    4. Si un ítem falla al publicar → skip + warning, continuar con el resto.
    5. Retornar lista de EventEnvelopes publicados exitosamente.

Manejo de errores parciales:
    - Error global (falla la consulta del perfil): loguear, no publicar señales falsas.
    - Error por ítem individual: skip + warning, procesar el resto.
    - Stories: se orquesta externamente vía execute_profile_stories_mission().
"""

from __future__ import annotations

import logging

from runtime.contracts.event_envelope import EventEnvelope
from runtime.contracts.mission import Mission
from runtime.engines.sensors.factory import DefaultSensorFactory
from runtime.engines.sensors.instagram_profile import InstagramProfileSensor
from runtime.events.bus import RedisEventBus
from runtime.events.publish_signal import wrap_and_publish
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


class ProfileCollectionOrchestrator:
    """
    Orquestador de recolección para misiones de perfil (target_type=profile).

    Produce un EventEnvelope por cada ítem recolectado (Posts + Reels),
    publicados en Redis bajo el mismo mission_id.
    """

    def __init__(
        self,
        sensor_factory: DefaultSensorFactory,
        event_bus: RedisEventBus,
    ) -> None:
        self._sensor_factory = sensor_factory
        self._event_bus = event_bus

    async def execute_profile_mission(
        self,
        mission: Mission,
    ) -> list[EventEnvelope]:
        """
        Ejecuta la recolección de Posts + Reels del perfil.

        Args:
            mission: Mission con target_type='profile'.

        Returns:
            Lista de EventEnvelopes publicados en Redis.
            Vacía si el perfil no devolvió contenido o todas las publicaciones fallaron.
        """
        if not mission.enabled:
            logger.info(
                "Profile mission skipped (disabled)",
                extra={"mission_id": str(mission.id), "username": mission.target},
            )
            return []

        logger.info(
            "Executing profile mission — Posts + Reels",
            extra={
                "mission_id": str(mission.id),
                "username": mission.target,
                "observation_scope": mission.observation_scope,
            },
        )

        try:
            sensor: InstagramProfileSensor = self._sensor_factory.build_profile_sensor(mission)
        except Exception as exc:
            logger.error(
                "Failed to build profile sensor",
                extra={
                    "mission_id": str(mission.id),
                    "username": mission.target,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return []

        # 1. Recolectar Posts + Reels
        try:
            raw_signals = await sensor.detect()
        except Exception as exc:
            logger.error(
                "Profile sensor detect() failed — no signals published",
                extra={
                    "mission_id": str(mission.id),
                    "username": mission.target,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return []

        if not raw_signals:
            logger.info(
                "Profile sensor returned no signals",
                extra={"mission_id": str(mission.id), "username": mission.target},
            )
            return []

        # 2. Publicar un EventEnvelope independiente por cada señal
        published: list[EventEnvelope] = []
        for i, raw_signal in enumerate(raw_signals):
            try:
                envelope = await wrap_and_publish(raw_signal, self._event_bus)
                published.append(envelope)
            except Exception as exc:
                logger.warning(
                    "Failed to publish signal item — skip",
                    extra={
                        "mission_id": str(mission.id),
                        "username": mission.target,
                        "item_index": i,
                        "error": str(exc),
                    },
                )

        logger.info(
            "Profile mission completed — signals published",
            extra={
                "mission_id": str(mission.id),
                "username": mission.target,
                "signals_collected": len(raw_signals),
                "signals_published": len(published),
            },
        )
        return published

    async def execute_profile_stories_mission(
        self,
        mission: Mission,
    ) -> list[EventEnvelope]:
        """
        Ejecuta la recolección de Stories del perfil.

        Llamado desde un job separado con story_interval_seconds (6h).
        Si stories falla completamente, retorna lista vacía (fallo soft).

        Args:
            mission: Mission con target_type='profile'.

        Returns:
            Lista de EventEnvelopes de stories publicados en Redis.
        """
        if not mission.enabled:
            logger.info(
                "Profile stories mission skipped (disabled)",
                extra={"mission_id": str(mission.id), "username": mission.target},
            )
            return []

        logger.info(
            "Executing profile mission — Stories",
            extra={
                "mission_id": str(mission.id),
                "username": mission.target,
            },
        )

        try:
            sensor: InstagramProfileSensor = self._sensor_factory.build_profile_sensor(mission)
        except Exception as exc:
            logger.error(
                "Failed to build profile sensor for stories",
                extra={"mission_id": str(mission.id), "error": str(exc)},
                exc_info=True,
            )
            return []

        # detect_stories() ya maneja su propio soft-failure internamente
        try:
            story_signals = await sensor.detect_stories()
        except Exception as exc:
            logger.warning(
                "Stories detection raised unexpected exception — skipping stories",
                extra={"mission_id": str(mission.id), "error": str(exc)},
                exc_info=True,
            )
            return []

        if not story_signals:
            logger.info(
                "No stories collected for profile",
                extra={"mission_id": str(mission.id), "username": mission.target},
            )
            return []

        published: list[EventEnvelope] = []
        for i, raw_signal in enumerate(story_signals):
            try:
                envelope = await wrap_and_publish(raw_signal, self._event_bus)
                published.append(envelope)
            except Exception as exc:
                logger.warning(
                    "Failed to publish story signal — skip",
                    extra={
                        "mission_id": str(mission.id),
                        "item_index": i,
                        "error": str(exc),
                    },
                )

        logger.info(
            "Profile stories mission completed",
            extra={
                "mission_id": str(mission.id),
                "username": mission.target,
                "stories_published": len(published),
            },
        )
        return published
