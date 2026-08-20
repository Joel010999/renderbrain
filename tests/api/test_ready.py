"""
Tests para GET /ready.

Tests unitarios: usan unittest.mock.patch para aislar las probes de
infraestructura real — no requieren PostgreSQL corriendo.

Test de integración: marcado con @pytest.mark.integration, ejecuta
las comprobaciones reales contra los contenedores Docker.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from runtime.api.main import app

client = TestClient(app)

_PATCH_PG = "runtime.api.main.check_postgres"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ready_response(pg: bool):
    """Ejecuta GET /ready con las probes mockeadas a los valores dados."""
    with patch(_PATCH_PG, new=AsyncMock(return_value=pg)):
        return client.get("/ready")


# ---------------------------------------------------------------------------
# Tests unitarios — sin infraestructura real
# ---------------------------------------------------------------------------

class TestReadySuccess:
    def test_returns_200_when_postgres_ok(self):
        """/ready debe devolver 200 si PostgreSQL responde."""
        response = _ready_response(pg=True)
        assert response.status_code == 200

    def test_body_status_is_ready(self):
        response = _ready_response(pg=True)
        assert response.json()["status"] == "ready"

    def test_body_postgres_is_ok(self):
        response = _ready_response(pg=True)
        assert response.json()["dependencies"]["postgres"] == "ok"

    def test_returns_200_when_redis_down_because_api_not_dependent(self):
        """A. DB OK + Redis down -> /ready 200, si Redis no es dependencia API."""
        # Aún si Redis falla o se inyecta como fallo, /ready solo verifica Postgres.
        with patch("runtime.api.main.check_redis", new=AsyncMock(return_value=False)):
            response = _ready_response(pg=True)
            assert response.status_code == 200
            assert response.json()["status"] == "ready"

    def test_ready_makes_zero_calls_to_llm_or_apify(self):
        """C. /ready hace cero llamadas OpenAI/Apify."""
        with patch("runtime.infrastructure.llm.openai.OpenAIAdapter") as mock_llm, \
             patch("runtime.infrastructure.apify.adapter.ApifyInstagramAdapter") as mock_apify:
            
            response = _ready_response(pg=True)
            assert response.status_code == 200
            
            assert not mock_llm.called
            assert not mock_apify.called


class TestReadyPostgresFails:
    def test_returns_503_when_postgres_fails(self):
        """/ready debe devolver 503 si PostgreSQL falla."""
        response = _ready_response(pg=False)
        assert response.status_code == 503

    def test_body_status_is_unavailable(self):
        response = _ready_response(pg=False)
        assert response.json()["status"] == "unavailable"

    def test_body_postgres_is_error(self):
        response = _ready_response(pg=False)
        assert response.json()["dependencies"]["postgres"] == "error"

    def test_returns_503_when_db_down_with_clear_error(self):
        """B. DB down -> /ready 503."""
        response = _ready_response(pg=False)
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"


class TestReadySecurityBoundary:
    """
    Verifica que el payload público nunca expone información sensible
    independientemente del resultado de las probes.
    """

    _SENSITIVE_PATTERNS = [
        "postgresql+asyncpg",
        "redis://",
        "localhost",
        "password",
        "DATABASE_URL",
        "REDIS_URL",
        "Traceback",
        "Exception",
        "Error:",
    ]

    def _assert_no_sensitive_data(self, response_text: str) -> None:
        for pattern in self._SENSITIVE_PATTERNS:
            assert pattern not in response_text, (
                f"El payload público contiene información sensible: '{pattern}'"
            )

    def test_success_response_has_no_sensitive_data(self):
        response = _ready_response(pg=True)
        self._assert_no_sensitive_data(response.text)

    def test_failure_response_has_no_sensitive_data(self):
        response = _ready_response(pg=False)
        self._assert_no_sensitive_data(response.text)


# ---------------------------------------------------------------------------
# Test de integración — requiere Docker con PostgreSQL healthy
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_ready_integration_both_healthy():
    """
    Prueba /ready contra la infraestructura real (sin mocks).

    Verifica que el endpoint retorna 200 y el payload correcto cuando
    los contenedores Docker están healthy.

    Prerequisito: docker compose up -d
    """
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        response = await ac.get("/ready")

    assert response.status_code == 200, (
        f"Se esperaba 200, obtenido {response.status_code}. "
        f"Body: {response.text}"
    )
    body = response.json()
    assert body["status"] == "ready"
    assert body["dependencies"]["postgres"] == "ok"
