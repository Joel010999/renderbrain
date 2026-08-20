"""
runtime/infrastructure/llm/interfaces.py

Protocolo estructural mínimo para proveedores LLM (S3.2).
"""

from typing import Protocol


class LLMProvider(Protocol):
    """
    Frontera mínima, asíncrona y segura para comunicación con LLMs.
    Recibe un prompt (str) y retorna una respuesta (str).
    """

    async def complete(self, prompt: str) -> str:
        """
        Envía un prompt al modelo y retorna la respuesta generada.
        """
        ...
