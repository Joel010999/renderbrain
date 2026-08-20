"""
runtime/engines/normalizer/__init__.py

Exporta NormalizerEngine para que el resto del sistema pueda importar desde aquí:
    from runtime.engines.normalizer import NormalizerEngine
"""

from runtime.engines.normalizer.engine import NormalizerEngine

__all__ = ["NormalizerEngine"]
