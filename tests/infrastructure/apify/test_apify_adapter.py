"""
Tests unitarios de ApifyInstagramAdapter — S2.1
================================================
NO realizan llamadas a Internet ni consumen créditos de Apify.
Todas las dependencias externas se mockean con unittest.mock.

Contrato real del SDK (apify-client>=3.x):
    client.actor(...).call() devuelve un objeto Run (Pydantic BaseModel),
    no un dict. Los atributos relevantes son:
        run.status              → str, p. ej. "SUCCEEDED"
        run.default_dataset_id → str, ID del dataset de resultados

Los mocks de esta suite reflejan ese contrato usando SimpleNamespace
(atributos, no claves de dict).

Cobertura:
    - URL vacía / esquema inválido
    - limit <= 0 y limit > 10
    - Token ausente en el entorno
    - Mapeo de resultado exitoso (happy path)
    - Dataset vacío
    - Fallo del Actor (status != SUCCEEDED)
    - Excepción inesperada del SDK
    - default_dataset_id ausente en la respuesta
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from runtime.infrastructure.apify.adapter import (
    ApifyActorRunError,
    ApifyEmptyDatasetError,
    ApifyInstagramAdapter,
    ApifyInvalidLimitError,
    ApifyInvalidURLError,
    ApifyTokenMissingError,
    ApifyUnexpectedResponseError,
)


# ---------------------------------------------------------------------------
# Constantes de apoyo
# ---------------------------------------------------------------------------

VALID_URL = "https://www.instagram.com/p/CTEST12345/"
SAMPLE_ITEM: dict[str, Any] = {
    "id": "CTest12345",
    "shortCode": "CTest12345",
    "url": VALID_URL,
    "likesCount": 42,
    "commentsCount": 7,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter_with_token(monkeypatch) -> ApifyInstagramAdapter:
    """Adaptador con token simulado en Settings; no llama a Apify."""
    from pydantic import SecretStr
    from runtime.shared import config as cfg_module

    monkeypatch.setattr(
        cfg_module.settings,
        "APIFY_API_TOKEN",
        SecretStr("fake-token-for-unit-tests"),
    )
    return ApifyInstagramAdapter()


@pytest.fixture()
def adapter_no_token(monkeypatch) -> ApifyInstagramAdapter:
    """Adaptador sin token configurado."""
    from runtime.shared import config as cfg_module

    monkeypatch.setattr(cfg_module.settings, "APIFY_API_TOKEN", None)
    return ApifyInstagramAdapter()


# ---------------------------------------------------------------------------
# Helpers para construir mocks del ApifyClient
#
# IMPORTANTE: apify-client>=3.x retorna un objeto Run (Pydantic BaseModel),
# no un dict. Usamos SimpleNamespace para simular el contrato real:
#   run.status              → str
#   run.default_dataset_id  → str
# ---------------------------------------------------------------------------

def _make_run_mock(
    status: str = "SUCCEEDED",
    default_dataset_id: str = "dataset-abc",
) -> SimpleNamespace:
    """Simula el objeto Run devuelto por apify-client>=3.x."""
    return SimpleNamespace(status=status, default_dataset_id=default_dataset_id)


def _make_client_mock(
    *,
    run_status: str = "SUCCEEDED",
    dataset_id: str = "dataset-abc",
    items: list[dict] | None = None,
    run_raises: Exception | None = None,
    dataset_raises: Exception | None = None,
) -> MagicMock:
    """Construye un MagicMock de ApifyClient con el comportamiento deseado."""
    client_mock = MagicMock()

    actor_mock = client_mock.actor.return_value
    if run_raises is not None:
        actor_mock.call.side_effect = run_raises
    else:
        # Retorna un objeto con atributos (contrato real del SDK, no dict)
        actor_mock.call.return_value = _make_run_mock(
            status=run_status,
            default_dataset_id=dataset_id,
        )

    dataset_mock = client_mock.dataset.return_value
    if dataset_raises is not None:
        dataset_mock.iterate_items.side_effect = dataset_raises
    else:
        dataset_mock.iterate_items.return_value = iter(items or [])

    return client_mock


def _make_run_mock_with_capture(
    captured_input: dict,
    *,
    dataset_id: str = "ds-capture",
) -> Any:
    """
    Retorna una función side_effect que captura run_input
    y devuelve un Run-like object con atributos.
    """
    def _call(run_input: dict) -> SimpleNamespace:
        captured_input.update(run_input)
        return _make_run_mock(default_dataset_id=dataset_id)

    return _call


# ---------------------------------------------------------------------------
# Validación de URL
# ---------------------------------------------------------------------------

class TestURLValidation:
    def test_empty_url_raises(self, adapter_with_token):
        with pytest.raises(ApifyInvalidURLError, match="vacía"):
            adapter_with_token.fetch_post("", limit=1)

    def test_blank_url_raises(self, adapter_with_token):
        with pytest.raises(ApifyInvalidURLError, match="vacía"):
            adapter_with_token.fetch_post("   ", limit=1)

    def test_non_http_scheme_raises(self, adapter_with_token):
        with pytest.raises(ApifyInvalidURLError, match="http/https"):
            adapter_with_token.fetch_post("ftp://example.com/post", limit=1)

    def test_no_scheme_raises(self, adapter_with_token):
        with pytest.raises(ApifyInvalidURLError):
            adapter_with_token.fetch_post("www.instagram.com/p/XXXX/", limit=1)

    def test_valid_http_url_passes_validation(self, adapter_with_token):
        """URL válida no lanza error de validación (el Actor se mockea)."""
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(items=[SAMPLE_ITEM]),
        ):
            result = adapter_with_token.fetch_post(
                "http://www.instagram.com/p/XXXX/", limit=1
            )
        assert result == [SAMPLE_ITEM]

    def test_valid_https_url_passes_validation(self, adapter_with_token):
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(items=[SAMPLE_ITEM]),
        ):
            result = adapter_with_token.fetch_post(VALID_URL, limit=1)
        assert result == [SAMPLE_ITEM]


# ---------------------------------------------------------------------------
# Validación de limit
# ---------------------------------------------------------------------------

class TestLimitValidation:
    def test_limit_zero_raises(self, adapter_with_token):
        with pytest.raises(ApifyInvalidLimitError, match=">="):
            adapter_with_token.fetch_post(VALID_URL, limit=0)

    def test_limit_negative_raises(self, adapter_with_token):
        with pytest.raises(ApifyInvalidLimitError, match=">="):
            adapter_with_token.fetch_post(VALID_URL, limit=-5)

    def test_limit_11_raises(self, adapter_with_token):
        with pytest.raises(ApifyInvalidLimitError, match="máximo"):
            adapter_with_token.fetch_post(VALID_URL, limit=11)

    def test_limit_100_raises(self, adapter_with_token):
        with pytest.raises(ApifyInvalidLimitError, match="máximo"):
            adapter_with_token.fetch_post(VALID_URL, limit=100)

    def test_limit_1_is_valid(self, adapter_with_token):
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(items=[SAMPLE_ITEM]),
        ):
            result = adapter_with_token.fetch_post(VALID_URL, limit=1)
        assert len(result) == 1

    def test_limit_10_is_valid(self, adapter_with_token):
        items = [SAMPLE_ITEM] * 10
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(items=items),
        ):
            result = adapter_with_token.fetch_post(VALID_URL, limit=10)
        assert len(result) == 10

    def test_default_limit_is_1(self, adapter_with_token):
        """El parámetro default debe ser 1 (costo mínimo)."""
        captured_input: dict = {}

        client_mock = MagicMock()
        client_mock.actor.return_value.call.side_effect = _make_run_mock_with_capture(
            captured_input, dataset_id="ds-1"
        )
        client_mock.dataset.return_value.iterate_items.return_value = iter([SAMPLE_ITEM])

        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=client_mock,
        ):
            adapter_with_token.fetch_post(VALID_URL)  # sin limit explícito

        assert captured_input.get("resultsLimit") == 1

    def test_results_limit_passed_to_actor(self, adapter_with_token):
        """resultsLimit debe coincidir con el limit solicitado."""
        captured_input: dict = {}

        client_mock = MagicMock()
        client_mock.actor.return_value.call.side_effect = _make_run_mock_with_capture(
            captured_input, dataset_id="ds-2"
        )
        client_mock.dataset.return_value.iterate_items.return_value = iter(
            [SAMPLE_ITEM] * 5
        )

        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=client_mock,
        ):
            adapter_with_token.fetch_post(VALID_URL, limit=5)

        assert captured_input["resultsLimit"] == 5


# ---------------------------------------------------------------------------
# Token ausente
# ---------------------------------------------------------------------------

class TestTokenMissing:
    def test_missing_token_raises_before_network_call(self, adapter_no_token):
        """Con token ausente, el error se lanza antes de cualquier llamada de red."""
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient"
        ) as client_cls:
            with pytest.raises(ApifyTokenMissingError):
                adapter_no_token.fetch_post(VALID_URL, limit=1)
            # El constructor de ApifyClient nunca debe haberse llamado
            client_cls.assert_not_called()

    def test_token_missing_error_message_has_no_secret(self, adapter_no_token):
        """El mensaje de error no debe contener secretos."""
        with pytest.raises(ApifyTokenMissingError) as exc_info:
            adapter_no_token.fetch_post(VALID_URL, limit=1)
        assert "APIFY_API_TOKEN" in str(exc_info.value)
        assert "fake" not in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Happy path — resultado exitoso
# ---------------------------------------------------------------------------

class TestSuccessfulFetch:
    def test_returns_list_of_dicts(self, adapter_with_token):
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(items=[SAMPLE_ITEM]),
        ):
            result = adapter_with_token.fetch_post(VALID_URL, limit=1)
        assert isinstance(result, list)
        assert all(isinstance(item, dict) for item in result)

    def test_returned_item_matches_mock(self, adapter_with_token):
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(items=[SAMPLE_ITEM]),
        ):
            result = adapter_with_token.fetch_post(VALID_URL, limit=1)
        assert result[0] == SAMPLE_ITEM

    def test_multiple_items_returned(self, adapter_with_token):
        items = [dict(SAMPLE_ITEM, id=str(i)) for i in range(3)]
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(items=items),
        ):
            result = adapter_with_token.fetch_post(VALID_URL, limit=3)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Dataset vacío
# ---------------------------------------------------------------------------

class TestEmptyDataset:
    def test_empty_dataset_raises(self, adapter_with_token):
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(items=[]),
        ):
            with pytest.raises(ApifyEmptyDatasetError):
                adapter_with_token.fetch_post(VALID_URL, limit=1)


# ---------------------------------------------------------------------------
# Errores del Actor
# ---------------------------------------------------------------------------

class TestActorErrors:
    def test_actor_run_failed_status_raises(self, adapter_with_token):
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(run_status="FAILED"),
        ):
            with pytest.raises(ApifyActorRunError, match="FAILED"):
                adapter_with_token.fetch_post(VALID_URL, limit=1)

    def test_actor_run_aborted_raises(self, adapter_with_token):
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(run_status="ABORTED"),
        ):
            with pytest.raises(ApifyActorRunError):
                adapter_with_token.fetch_post(VALID_URL, limit=1)

    def test_actor_sdk_exception_wrapped(self, adapter_with_token):
        """Excepción del SDK debe convertirse en ApifyActorRunError."""
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(
                run_raises=RuntimeError("SDK internal error")
            ),
        ):
            with pytest.raises(ApifyActorRunError):
                adapter_with_token.fetch_post(VALID_URL, limit=1)

    def test_actor_sdk_exception_does_not_expose_token(self, adapter_with_token):
        """El mensaje de ApifyActorRunError no debe contener el token."""
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(
                run_raises=RuntimeError("connection refused")
            ),
        ):
            with pytest.raises(ApifyActorRunError) as exc_info:
                adapter_with_token.fetch_post(VALID_URL, limit=1)
        assert "fake-token" not in str(exc_info.value)

    def test_missing_dataset_id_raises(self, adapter_with_token):
        """Si default_dataset_id está vacío, debe lanzar ApifyUnexpectedResponseError."""
        client_mock = MagicMock()
        # Run object con default_dataset_id vacío/None
        client_mock.actor.return_value.call.return_value = SimpleNamespace(
            status="SUCCEEDED",
            default_dataset_id="",  # vacío — debe disparar el error
        )
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=client_mock,
        ):
            with pytest.raises(ApifyUnexpectedResponseError, match="default_dataset_id"):
                adapter_with_token.fetch_post(VALID_URL, limit=1)

    def test_dataset_read_exception_wrapped(self, adapter_with_token):
        """Error al leer el dataset debe convertirse en ApifyUnexpectedResponseError."""
        with patch(
            "runtime.infrastructure.apify.adapter.ApifyClient",
            return_value=_make_client_mock(
                dataset_raises=ConnectionError("network error")
            ),
        ):
            with pytest.raises(ApifyUnexpectedResponseError):
                adapter_with_token.fetch_post(VALID_URL, limit=1)
