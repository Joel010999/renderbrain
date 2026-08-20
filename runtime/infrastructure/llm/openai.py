"""
runtime/infrastructure/llm/openai.py

Implementación concreta del LLMProvider utilizando el SDK oficial de OpenAI.
Garantiza que ningún objeto o excepción del SDK filtre hacia las capas superiores (S3.2).
"""

from openai import AsyncOpenAI
from openai import APIError, APIConnectionError, RateLimitError, AuthenticationError

from runtime.shared.config import settings
from runtime.infrastructure.llm.interfaces import LLMProvider
from runtime.infrastructure.llm.errors import (
    LLMConfigError,
    LLMInputError,
    LLMProviderError,
    LLMEmptyResponseError,
)


class OpenAIAdapter(LLMProvider):
    """
    Adaptador para OpenAI que implementa la interfaz estructural LLMProvider.
    """

    def __init__(self) -> None:
        if not settings.OPENAI_API_KEY:
            raise LLMConfigError("OPENAI_API_KEY no está configurada en el entorno.")
            
        # get_secret_value() extrae el string subyacente para pasarlo al SDK.
        self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())
        self._model = settings.OPENAI_MODEL

    async def complete(self, prompt: str) -> str:
        if not prompt or not prompt.strip():
            raise LLMInputError("El prompt proporcionado está vacío o es inválido.")

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
        except AuthenticationError as e:
            # Capturamos error de auth para no exponer detalles de key y mapearlo a LLMConfigError
            raise LLMConfigError("Fallo de autenticación con OpenAI. Verifica la API key.") from e
        except (APIError, APIConnectionError, RateLimitError) as e:
            raise LLMProviderError(f"Error de red o de servicio con OpenAI: {e.__class__.__name__}") from e
        except Exception as e:
            raise LLMProviderError("Error inesperado al comunicarse con OpenAI.") from e

        if not response.choices:
            raise LLMEmptyResponseError("La respuesta de OpenAI no contiene opciones (choices vacío).")

        content = response.choices[0].message.content
        if not content:
            raise LLMEmptyResponseError("La respuesta de OpenAI contiene un mensaje vacío.")

        return content
