"""
runtime/contracts/interfaces.py

Interfaces abstractas para el First Signal Flow.

ABCs puras sin lógica de infraestructura. Definen los contratos
que deben cumplir los sensores y el normalizador.
"""

from abc import ABC, abstractmethod

from runtime.contracts.canonical_signal import CanonicalSignal, CanonicalSignalData
from runtime.contracts.raw_signal_detected import RawSignalDetected


class BaseSensor(ABC):
    """
    Interfaz abstracta para todos los sensores de RenderBrain.
    """

    @abstractmethod
    async def detect(self) -> RawSignalDetected:
        """
        Produce un RawSignalDetected desde una fuente de datos concreta.
        """
        pass


class BaseNormalizer(ABC):
    """
    Interfaz abstracta para el motor de normalización.
    """

    @abstractmethod
    async def normalize(self, signal: RawSignalDetected) -> CanonicalSignalData:
        """
        Transforma un RawSignalDetected en un CanonicalSignalData.
        """
        pass
