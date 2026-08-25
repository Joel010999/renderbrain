"""
runtime/engines/sensors/factory.py

Factory para resolver el sensor apropiado basado en la configuración de la misión.

A1.1 — Extensión:
    - build_sensor(): sigue resolviendo target_type=post (legado S2.1).
    - build_profile_sensor(): nuevo — resuelve target_type=profile.
    - La selección del tipo de sensor se hace en el orquestador (mission_scheduler)
      basándose en mission.target_type.
"""

from typing import Protocol
from uuid import UUID

from runtime.contracts.interfaces import BaseSensor
from runtime.contracts.mission import Mission
from runtime.engines.sensors.instagram import InstagramSensor
from runtime.engines.sensors.instagram_profile import InstagramProfileSensor
from runtime.infrastructure.apify.adapter import ApifyInstagramAdapter
from runtime.shared.config import settings


SUPPORTED_SENSOR_SOURCES = frozenset({"instagram"})

class SensorFactory(Protocol):
    """Interfaz para la creación de sensores a partir de misiones."""

    def build_sensor(self, mission: Mission) -> BaseSensor:
        """Construye un BaseSensor instanciado según el origen y target de la misión."""
        ...


class DefaultSensorFactory:
    """Implementación por defecto que resuelve sensores reales."""

    def build_sensor(self, mission: Mission) -> BaseSensor:
        """
        Resuelve el sensor para target_type='post' (legado S2.1).

        Args:
            mission: La misión activa con target_type='post'.

        Raises:
            NotImplementedError: Si el source de la misión no está soportado.
            ValueError: Si se intenta usar para target_type='profile'.
        """
        if mission.source not in SUPPORTED_SENSOR_SOURCES:
            raise NotImplementedError(
                f"No existe un sensor registrado para el source '{mission.source}'"
            )

        if mission.target_type == "profile":
            raise ValueError(
                "build_sensor() es para target_type='post'. "
                "Usar build_profile_sensor() para misiones de perfil."
            )

        if mission.source == "instagram":
            # Inyecta el adaptador real de Apify para Instagram
            adapter = ApifyInstagramAdapter()
            return InstagramSensor(
                mission_id=mission.id,
                url=mission.target,
                adapter=adapter,
            )

    def build_profile_sensor(self, mission: Mission) -> InstagramProfileSensor:
        """
        Resuelve el sensor de perfil para target_type='profile' (A1.1).

        Args:
            mission: La misión activa con target_type='profile'.

        Returns:
            InstagramProfileSensor configurado con límites desde settings.

        Raises:
            NotImplementedError: Si el source de la misión no está soportado.
            ValueError: Si se intenta usar para target_type='post'.
        """
        if mission.source not in SUPPORTED_SENSOR_SOURCES:
            raise NotImplementedError(
                f"No existe un sensor de perfil registrado para el source '{mission.source}'"
            )

        if mission.target_type != "profile":
            raise ValueError(
                "build_profile_sensor() es para target_type='profile'. "
                "Usar build_sensor() para misiones de post individual."
            )

        if mission.source == "instagram":
            adapter = ApifyInstagramAdapter()
            return InstagramProfileSensor(
                mission_id=mission.id,
                username=mission.target,
                adapter=adapter,
                post_limit=settings.INSTAGRAM_PROFILE_POST_LIMIT,
                reel_limit=settings.INSTAGRAM_PROFILE_REEL_LIMIT,
                story_limit=settings.INSTAGRAM_PROFILE_STORY_LIMIT,
            )
