"""
Test externo opt-in — S2.1
===========================
Llama REALMENTE a Apify usando credenciales del .env.
CONSUME CRÉDITOS: ejecutar solo cuando sea necesario.

Cómo ejecutar (requiere opt-in explícito):
    uv run pytest --run-external -m external -v

Este test es EXCLUIDO de cualquier invocación habitual de pytest gracias
al hook pytest_collection_modifyitems en conftest.py (raíz). El flag
--run-external es el único mecanismo para habilitarlo.

Requisito: APIFY_API_TOKEN debe estar presente en .env.
Si el token no está configurado, el test emite pytest.skip() limpio.

URL de prueba:
    Post público directo de @nasa en Instagram.
    Se usa una URL de post (/p/) en lugar de un perfil para garantizar
    que el Actor procese contenido concreto y devuelva resultados.
    limit=1 para consumo mínimo de créditos.
"""

from __future__ import annotations

import pytest

from runtime.shared.config import settings


# Post público verificado de @nasa — foto icónica, cuenta verificada,
# sin login requerido. URL de post directo (/p/) para garantizar resultados.
_TEST_URL = "https://www.instagram.com/p/B8rk0ISnDT5/"


@pytest.mark.external
def test_fetch_post_real_apify_call() -> None:
    """Llama a Apify con limit=1 y un post público directo de @nasa.

    Verifica únicamente que:
    - El adaptador retorna una lista no vacía.
    - Cada ítem es un dict.
    No valida campos específicos para desacoplarse de la respuesta exacta del Actor.
    """
    # Skip limpio si no hay token (evita fallos en CI sin credenciales)
    if settings.APIFY_API_TOKEN is None:
        pytest.skip(
            "APIFY_API_TOKEN no configurado en .env — test externo omitido."
        )

    from runtime.infrastructure.apify.adapter import ApifyInstagramAdapter

    adapter = ApifyInstagramAdapter()
    result = adapter.fetch_post(_TEST_URL, limit=1)

    assert isinstance(result, list), "Se esperaba una lista como resultado"
    assert len(result) >= 1, "Se esperaba al menos 1 ítem en el dataset"
    assert all(isinstance(item, dict) for item in result), (
        "Todos los ítems deben ser dicts"
    )

    # Reportar éxito sin imprimir contenido sensible
    print(f"\n[external] Apify devolvió {len(result)} ítem(s) para {_TEST_URL}. Test OK.")
