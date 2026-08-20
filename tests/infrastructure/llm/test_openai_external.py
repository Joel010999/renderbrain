"""
tests/infrastructure/llm/test_openai_external.py

Test real contra OpenAI (S3.2). Opt-in explícito.
"""

import pytest

from runtime.infrastructure.llm.openai import OpenAIAdapter
from runtime.shared.config import settings


@pytest.mark.external
@pytest.mark.asyncio
async def test_openai_real_call():
    """
    Test E2E contra la API de OpenAI.
    Consume créditos, ejecutar solo bajo demanda con --run-external.
    """
    if not settings.OPENAI_API_KEY:
        pytest.skip("OPENAI_API_KEY no configurada — test externo omitido.")

    adapter = OpenAIAdapter()
    prompt = "Responde exactamente con la palabra 'HOLA' y nada más."

    # Se usa el modelo por defecto rápido y económico (gpt-4o-mini)
    result = await adapter.complete(prompt)

    assert isinstance(result, str)
    assert len(result) > 0
    assert "HOLA" in result.upper()
