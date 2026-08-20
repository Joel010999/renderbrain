"""
Tests unitarios de InstagramSensor — S2.2
==========================================
NO realizan llamadas a Apify ni consumen créditos.
El ApifyInstagramAdapter se simula con un FakeAdapter simple.

Filosofía de testing:
    Se usa un FakeAdapter (objeto plano con método fetch_post) en lugar de
    MagicMock para que los tests sean más legibles y el contrato del adaptador
    quede explícito. MagicMock solo se usa donde necesitamos side_effect.

Casos cubiertos:
    1. Happy path: 1 ítem → RawSignalDetected correcto.
    2. source es siempre "instagram" (no "apify" ni el nombre del SDK).
    3. mission_id se preserva sin alteración.
    4. captured_at es timezone-aware en UTC.
    5. raw_payload contiene url_queried, items_received y data íntegros.
    6. Lista vacía desde el adapter → InstagramSensorEmptyResultError.
    7. Excepción del adapter → InstagramSensorAdapterError (sin secretos).
    8. sensor name es "instagram_apify_sensor".
    9. El sensor NO hace I/O fuera del adapter (adapter.fetch_post es el único
       punto de contacto con infraestructura).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from runtime.engines.sensors.instagram import (
    SENSOR_NAME,
    SOURCE_NAME,
    InstagramSensor,
    InstagramSensorAdapterError,
    InstagramSensorEmptyResultError,
)


# ---------------------------------------------------------------------------
# Fakes y helpers
# ---------------------------------------------------------------------------

class FakeAdapter:
    """Fake de ApifyInstagramAdapter para tests unitarios.

    Implementa la misma interfaz pública que ApifyInstagramAdapter:
        fetch_post(url: str, limit: int = 1) -> list[dict]

    No importa apify-client ni realiza ninguna llamada de red.
    """

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        # Registra las llamadas para verificar que el sensor interactúa
        # correctamente con el adaptador.
        self.call_count: int = 0
        self.last_url: str | None = None
        self.last_limit: int | None = None

    def fetch_post(self, url: str, limit: int = 1) -> list[dict[str, Any]]:
        self.call_count += 1
        self.last_url = url
        self.last_limit = limit
        return self._items


SAMPLE_URL = "https://www.instagram.com/p/CTEST12345/"
SAMPLE_ITEM: dict[str, Any] = {
    "id": "CTest12345",
    "shortCode": "CTest12345",
    "url": SAMPLE_URL,
    "likesCount": 42,
    "commentsCount": 7,
    "caption": "Test caption",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mission_id() -> UUID:
    return uuid4()


@pytest.fixture()
def adapter_one_item() -> FakeAdapter:
    return FakeAdapter(items=[SAMPLE_ITEM])


@pytest.fixture()
def adapter_empty() -> FakeAdapter:
    return FakeAdapter(items=[])


# ---------------------------------------------------------------------------
# 1. Happy path — resultado exitoso con 1 ítem
# ---------------------------------------------------------------------------

class TestHappyPath:
    async def test_returns_raw_signal_detected(self, mission_id, adapter_one_item):
        """detect() debe retornar una instancia de RawSignalDetected."""
        from runtime.contracts.raw_signal_detected import RawSignalDetected

        sensor = InstagramSensor(
            mission_id=mission_id,
            url=SAMPLE_URL,
            adapter=adapter_one_item,
        )
        result = await sensor.detect()

        assert isinstance(result, RawSignalDetected)

    async def test_source_is_instagram(self, mission_id, adapter_one_item):
        """source debe ser 'instagram', no el nombre del proveedor técnico."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        result = await sensor.detect()

        assert result.source == SOURCE_NAME == "instagram"

    async def test_sensor_name_is_correct(self, mission_id, adapter_one_item):
        """sensor debe identificar la implementación: 'instagram_apify_sensor'."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        result = await sensor.detect()

        assert result.sensor == SENSOR_NAME == "instagram_apify_sensor"

    async def test_mission_id_preserved(self, mission_id, adapter_one_item):
        """mission_id debe preservarse sin alteración en el RawSignalDetected."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        result = await sensor.detect()

        assert result.mission_id == mission_id

    async def test_captured_at_is_timezone_aware(self, mission_id, adapter_one_item):
        """captured_at debe ser timezone-aware en UTC."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        result = await sensor.detect()

        assert result.captured_at is not None
        assert result.captured_at.tzinfo is not None
        assert result.captured_at.tzinfo == UTC

    async def test_raw_payload_contains_url_queried(self, mission_id, adapter_one_item):
        """raw_payload debe incluir url_queried para trazabilidad."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        result = await sensor.detect()

        assert "url_queried" in result.raw_payload
        assert result.raw_payload["url_queried"] == SAMPLE_URL

    async def test_raw_payload_contains_items_received(self, mission_id, adapter_one_item):
        """raw_payload debe incluir items_received para trazabilidad."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        result = await sensor.detect()

        assert "items_received" in result.raw_payload
        assert result.raw_payload["items_received"] == 1

    async def test_raw_payload_data_contains_apify_item(self, mission_id, adapter_one_item):
        """raw_payload['data'] debe contener íntegramente el ítem crudo de Apify."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        result = await sensor.detect()

        assert "data" in result.raw_payload
        assert result.raw_payload["data"] == SAMPLE_ITEM

    async def test_raw_payload_data_is_not_transformed(self, mission_id, adapter_one_item):
        """El sensor NO debe modificar ni normalizar los campos del ítem crudo."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        result = await sensor.detect()

        # Los campos originales de Apify deben aparecer sin alteración
        data = result.raw_payload["data"]
        assert data["likesCount"] == SAMPLE_ITEM["likesCount"]
        assert data["caption"] == SAMPLE_ITEM["caption"]
        assert data["shortCode"] == SAMPLE_ITEM["shortCode"]


# ---------------------------------------------------------------------------
# 2. Interacción con el adapter
# ---------------------------------------------------------------------------

class TestAdapterInteraction:
    async def test_adapter_fetch_post_called_once(self, mission_id, adapter_one_item):
        """El sensor debe llamar a fetch_post exactamente una vez."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        await sensor.detect()

        assert adapter_one_item.call_count == 1

    async def test_adapter_called_with_correct_url(self, mission_id, adapter_one_item):
        """El sensor debe pasar la URL correcta al adaptador."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        await sensor.detect()

        assert adapter_one_item.last_url == SAMPLE_URL

    async def test_adapter_called_with_limit_1(self, mission_id, adapter_one_item):
        """El sensor debe usar limit=1 para minimizar el costo de Apify."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_one_item
        )
        await sensor.detect()

        assert adapter_one_item.last_limit == 1

    async def test_sensor_does_not_import_apify_client(self):
        """Verificación estática: instagram.py no debe importar apify_client."""
        import ast
        import pathlib

        sensor_path = pathlib.Path(
            "runtime/engines/sensors/instagram.py"
        )
        source = sensor_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    assert "apify_client" not in name, (
                        f"instagram.py no debe importar apify_client, "
                        f"pero se encontró: {name}"
                    )


# ---------------------------------------------------------------------------
# 3. Dataset vacío → InstagramSensorEmptyResultError
# ---------------------------------------------------------------------------

class TestEmptyResult:
    async def test_empty_items_raises_empty_result_error(
        self, mission_id, adapter_empty
    ):
        """Lista vacía del adapter debe lanzar InstagramSensorEmptyResultError."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_empty
        )
        with pytest.raises(InstagramSensorEmptyResultError):
            await sensor.detect()

    async def test_empty_result_error_message_contains_url(
        self, mission_id, adapter_empty
    ):
        """El mensaje de error debe mencionar la URL para facilitar el diagnóstico."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_empty
        )
        with pytest.raises(InstagramSensorEmptyResultError, match=SAMPLE_URL):
            await sensor.detect()

    async def test_empty_result_error_does_not_contain_secrets(
        self, mission_id, adapter_empty
    ):
        """El mensaje de error no debe contener tokens ni credenciales."""
        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=adapter_empty
        )
        with pytest.raises(InstagramSensorEmptyResultError) as exc_info:
            await sensor.detect()
        # Ningún patrón típico de token debe aparecer en el mensaje
        error_msg = str(exc_info.value).lower()
        assert "token" not in error_msg
        assert "apify_api" not in error_msg
        assert "secret" not in error_msg


# ---------------------------------------------------------------------------
# 4. Error del adapter → InstagramSensorAdapterError
# ---------------------------------------------------------------------------

class TestAdapterError:
    async def test_adapter_exception_raises_adapter_error(self, mission_id):
        """Excepción del adapter debe convertirse en InstagramSensorAdapterError."""
        failing_adapter = MagicMock()
        failing_adapter.fetch_post.side_effect = RuntimeError("network timeout")

        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=failing_adapter
        )
        with pytest.raises(InstagramSensorAdapterError):
            await sensor.detect()

    async def test_adapter_error_wraps_original_exception(self, mission_id):
        """InstagramSensorAdapterError debe encadenar la causa original."""
        original_exc = ConnectionError("Apify unreachable")
        failing_adapter = MagicMock()
        failing_adapter.fetch_post.side_effect = original_exc

        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=failing_adapter
        )
        with pytest.raises(InstagramSensorAdapterError) as exc_info:
            await sensor.detect()

        assert exc_info.value.__cause__ is original_exc

    async def test_adapter_error_message_contains_url(self, mission_id):
        """El mensaje de error debe incluir la URL para facilitar diagnóstico."""
        failing_adapter = MagicMock()
        failing_adapter.fetch_post.side_effect = RuntimeError("error")

        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=failing_adapter
        )
        with pytest.raises(InstagramSensorAdapterError, match=SAMPLE_URL):
            await sensor.detect()

    async def test_adapter_error_does_not_expose_token(self, mission_id):
        """InstagramSensorAdapterError no debe exponer tokens en el mensaje."""
        # Simula una excepción que menciona un token (escenario adverso)
        failing_adapter = MagicMock()
        failing_adapter.fetch_post.side_effect = RuntimeError(
            "Auth failed: token=abc123secret"
        )

        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=failing_adapter
        )
        with pytest.raises(InstagramSensorAdapterError) as exc_info:
            await sensor.detect()

        # El sensor debe envolver la excepción, no propagar su mensaje crudo
        # — el mensaje del wrapper solo contiene type(exc).__name__, no el valor
        error_msg = str(exc_info.value)
        assert "abc123secret" not in error_msg

    async def test_apify_token_missing_error_wrapped(self, mission_id):
        """ApifyTokenMissingError del SDK también debe convertirse en error de dominio."""
        from runtime.infrastructure.apify.adapter import ApifyTokenMissingError

        failing_adapter = MagicMock()
        failing_adapter.fetch_post.side_effect = ApifyTokenMissingError(
            "APIFY_API_TOKEN no configurado"
        )

        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=failing_adapter
        )
        with pytest.raises(InstagramSensorAdapterError):
            await sensor.detect()

    async def test_apify_empty_dataset_wrapped(self, mission_id):
        """ApifyEmptyDatasetError del SDK debe ser capturado y re-lanzado."""
        from runtime.infrastructure.apify.adapter import ApifyEmptyDatasetError

        failing_adapter = MagicMock()
        failing_adapter.fetch_post.side_effect = ApifyEmptyDatasetError(
            "Dataset vacío"
        )

        sensor = InstagramSensor(
            mission_id=mission_id, url=SAMPLE_URL, adapter=failing_adapter
        )
        # ApifyEmptyDatasetError viene del adapter, no de detect() — se convierte
        # en InstagramSensorAdapterError (no EmptyResult, que es lista [] de Python)
        with pytest.raises(InstagramSensorAdapterError):
            await sensor.detect()
