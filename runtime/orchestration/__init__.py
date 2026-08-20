# runtime/orchestration/__init__.py
#
# Importar desde aquí:
#   from runtime.orchestration.signal_flow import run_signal_flow
from runtime.orchestration.signal_flow import run_signal_flow
from runtime.orchestration.cognitive_flow import run_cognitive_flow

__all__ = ["run_signal_flow", "run_cognitive_flow"]
