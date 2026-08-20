# runtime/contracts — Contratos públicos de RenderBrain
#
# Importar desde aquí para no depender de rutas internas:
#   from runtime.contracts import EventEnvelope

from runtime.contracts.canonical_signal import CanonicalSignal, CanonicalSignalData
from runtime.contracts.event_envelope import EventEnvelope
from runtime.contracts.interfaces import BaseNormalizer, BaseSensor
from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.contracts.knowledge import Evidence, Insight, KnowledgeTransaction

__all__ = [
    "EventEnvelope",
    "RawSignalDetected",
    "CanonicalSignal",
    "CanonicalSignalData",
    "BaseSensor",
    "BaseNormalizer",
    "Evidence",
    "Insight",
    "KnowledgeTransaction",
]
