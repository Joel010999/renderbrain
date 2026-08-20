"""
conftest.py — raíz del proyecto RenderBrain
============================================
Configuración global de pytest.

Mecanismo de opt-in para tests @pytest.mark.external
-----------------------------------------------------
Los tests marcados con @pytest.mark.external realizan llamadas reales a APIs
externas y consumen créditos. Están deshabilitados en todas las invocaciones
habituales de pytest, incluyendo:

    uv run pytest                        # suite normal
    uv run pytest -m "not integration"   # excluye infra, pero NO habilita external
    uv run pytest tests/...              # ruta explícita
    uv run pytest -k "..."               # filtro por nombre

Para habilitarlos se requiere el flag explícito --run-external:

    uv run pytest --run-external -m external -v

Implementación:
    pytest_collection_modifyitems() inspecciona cada ítem colectado y añade
    pytest.mark.skip si el test tiene el marcador 'external' y el flag
    --run-external NO fue pasado. Esto opera independientemente de cualquier
    expresión -m, por lo que no puede ser anulado accidentalmente.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Registra el flag --run-external en el CLI de pytest."""
    parser.addoption(
        "--run-external",
        action="store_true",
        default=False,
        help=(
            "Habilita los tests @pytest.mark.external que realizan llamadas "
            "reales a APIs externas y consumen créditos. "
            "Uso: uv run pytest --run-external -m external -v"
        ),
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Marca con skip todos los tests 'external' salvo opt-in explícito.

    Se ejecuta después de la colección completa, independientemente de
    cualquier expresión -m pasada en la línea de comandos. Esto garantiza
    que --run-external sea el único mecanismo para habilitarlos.
    """
    if config.getoption("--run-external"):
        # Opt-in explícito: no se modifica nada, los tests corren normalmente.
        return

    skip_external = pytest.mark.skip(
        reason=(
            "Test externo omitido. "
            "Para ejecutarlo: uv run pytest --run-external -m external -v"
        )
    )

    for item in items:
        if item.get_closest_marker("external") is not None:
            item.add_marker(skip_external)
