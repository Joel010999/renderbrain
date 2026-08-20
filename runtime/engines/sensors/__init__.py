# runtime/engines/sensors — Sensores de captura de RenderBrain
#
# Importar desde aquí:
#   from runtime.engines.sensors import ManualSensor
#   from runtime.engines.sensors import InstagramSensor

from runtime.engines.sensors.instagram import InstagramSensor
from runtime.engines.sensors.manual import ManualSensor

__all__ = ["ManualSensor", "InstagramSensor"]

