"""
runtime/engines/sensors/factory.py

Factory para resolver el sensor apropiado basado en la configuración de la misión.
"""

from typing import Protocol
from uuid import UUID

from runtime.contracts.interfaces import BaseSensor
from runtime.contracts.mission import Mission
from runtime.engines.sensors.instagram import InstagramSensor
from runtime.infrastructure.apify.adapter import ApifyInstagramAdapter


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
        Resuelve el sensor.
        
        Args:
            mission: La misión activa. El source define el tipo de sensor,
                     y el target proporciona la URL/input.
                     
        Raises:
            NotImplementedError: Si el source de la misión no está soportado.
        """
        if mission.source not in SUPPORTED_SENSOR_SOURCES:
            raise NotImplementedError(
                f"No existe un sensor registrado para el source '{mission.source}'"
            )

        if mission.source == "instagram":
            # Inyecta el adaptador real de Apify para Instagram
            adapter = ApifyInstagramAdapter()
            return InstagramSensor(
                mission_id=mission.id,
                url=mission.target,
                adapter=adapter,
            )
