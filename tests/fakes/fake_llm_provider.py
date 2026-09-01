"""
tests/fakes/fake_llm_provider.py

Fake provider para tests offline del LLM (S3.2).
"""

from runtime.infrastructure.llm.interfaces import LLMProvider
from runtime.infrastructure.llm.errors import LLMInputError


class FakeLLMProvider(LLMProvider):
    """
    Simula un LLMProvider devolviendo una respuesta predefinida.
    """
    def __init__(self, predefined_response: str = "Fake response") -> None:
        self.predefined_response = predefined_response
        self.call_count = 0

    async def complete(self, prompt: str) -> str:
        self.call_count += 1
        if not prompt or not prompt.strip():
            raise LLMInputError("El prompt no puede estar vacío.")
        if "QA Reviewer" in prompt:
            return '{"is_aligned": true, "reason": "ok"}'
        return self.predefined_response
