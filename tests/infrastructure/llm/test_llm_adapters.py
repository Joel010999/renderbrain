"""
tests/infrastructure/llm/test_llm_adapters.py

Tests offline para los adaptadores LLM (S3.2).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import AuthenticationError

from runtime.infrastructure.llm.errors import (
    LLMConfigError,
    LLMEmptyResponseError,
    LLMInputError,
    LLMProviderError,
)
from runtime.infrastructure.llm.openai import OpenAIAdapter
from runtime.shared.config import settings
from tests.fakes.fake_llm_provider import FakeLLMProvider


@pytest.mark.asyncio
async def test_fake_llm_provider_returns_predefined():
    provider = FakeLLMProvider("Respuesta esperada")
    result = await provider.complete("Hola")
    assert result == "Respuesta esperada"


@pytest.mark.asyncio
async def test_fake_llm_provider_rejects_empty_prompt():
    provider = FakeLLMProvider()
    with pytest.raises(LLMInputError):
        await provider.complete("   ")


@pytest.fixture
def mock_openai_client():
    with patch("runtime.infrastructure.llm.openai.AsyncOpenAI") as mock:
        yield mock


@pytest.mark.asyncio
async def test_openai_adapter_validates_api_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    with pytest.raises(LLMConfigError, match="OPENAI_API_KEY no está configurada"):
        OpenAIAdapter()


@pytest.mark.asyncio
async def test_openai_adapter_rejects_empty_prompt(monkeypatch):
    monkeypatch.setattr(
        settings, "OPENAI_API_KEY", MagicMock(get_secret_value=lambda: "test_key")
    )
    adapter = OpenAIAdapter()
    with pytest.raises(LLMInputError):
        await adapter.complete("")


@pytest.mark.asyncio
async def test_openai_adapter_successful_response(mock_openai_client, monkeypatch):
    monkeypatch.setattr(
        settings, "OPENAI_API_KEY", MagicMock(get_secret_value=lambda: "test_key")
    )

    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "OpenAI response"
    mock_response.choices = [mock_choice]

    mock_client_instance = mock_openai_client.return_value
    mock_client_instance.chat.completions.create = AsyncMock(return_value=mock_response)

    adapter = OpenAIAdapter()
    result = await adapter.complete("Test prompt")

    assert result == "OpenAI response"
    mock_client_instance.chat.completions.create.assert_called_once()


@pytest.mark.asyncio
async def test_openai_adapter_wraps_auth_error(mock_openai_client, monkeypatch):
    monkeypatch.setattr(
        settings, "OPENAI_API_KEY", MagicMock(get_secret_value=lambda: "test_key")
    )

    mock_client_instance = mock_openai_client.return_value
    auth_err = AuthenticationError(
        message="Invalid API Key", 
        response=MagicMock(), 
        body=None
    )
    mock_client_instance.chat.completions.create = AsyncMock(side_effect=auth_err)

    adapter = OpenAIAdapter()
    with pytest.raises(LLMConfigError, match="Fallo de autenticación con OpenAI"):
        await adapter.complete("Prompt")


@pytest.mark.asyncio
async def test_openai_adapter_wraps_generic_exceptions(mock_openai_client, monkeypatch):
    monkeypatch.setattr(
        settings, "OPENAI_API_KEY", MagicMock(get_secret_value=lambda: "test_key")
    )

    mock_client_instance = mock_openai_client.return_value
    mock_client_instance.chat.completions.create = AsyncMock(side_effect=Exception("Crash"))

    adapter = OpenAIAdapter()
    with pytest.raises(LLMProviderError, match="Error inesperado al comunicarse"):
        await adapter.complete("Prompt")


@pytest.mark.asyncio
async def test_openai_adapter_empty_response(mock_openai_client, monkeypatch):
    monkeypatch.setattr(
        settings, "OPENAI_API_KEY", MagicMock(get_secret_value=lambda: "test_key")
    )

    mock_response = MagicMock()
    mock_response.choices = []

    mock_client_instance = mock_openai_client.return_value
    mock_client_instance.chat.completions.create = AsyncMock(return_value=mock_response)

    adapter = OpenAIAdapter()
    with pytest.raises(LLMEmptyResponseError, match="no contiene opciones"):
        await adapter.complete("Prompt")
