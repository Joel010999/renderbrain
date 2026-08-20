"""
runtime/infrastructure/llm/errors.py

Errores mínimos y controlados para la frontera LLM (S3.2).
"""


class LLMError(Exception):
    """Clase base para todos los errores de la capa LLM."""
    pass


class LLMConfigError(LLMError):
    """Falta de configuración (ej. API Key ausente) o error de autenticación."""
    pass


class LLMInputError(LLMError):
    """Input inválido (ej. prompt vacío)."""
    pass


class LLMProviderError(LLMError):
    """Fallo en el servicio del proveedor (timeout, rate limit, API errors)."""
    pass


class LLMEmptyResponseError(LLMError):
    """La respuesta del proveedor fue vacía o no contenía el formato esperado."""
    pass
