"""
Test de integración — Conexión real a PostgreSQL.

Requisito previo: contenedor renderbrain-postgres corriendo y healthy.

    docker compose -f docker-compose.db.yml up -d
    uv run pytest tests/integration -v -m integration

Este test NO debe ejecutarse junto con los tests unitarios en CI/CD
a menos que haya un servicio PostgreSQL disponible.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from runtime.shared.config import settings


@pytest.mark.integration
async def test_select_one_returns_one():
    """
    Abre una conexión real al PostgreSQL configurado en DATABASE_URL,
    ejecuta SELECT 1 y verifica que el resultado sea 1.

    Valida que:
    - El engine se conecta exitosamente a la instancia Docker.
    - El driver asyncpg funciona correctamente de extremo a extremo.
    - Las credenciales del .env son válidas para el contenedor.
    """
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        # Pool mínimo para tests: una sola conexión, cerrada al finalizar
        pool_size=1,
        max_overflow=0,
    )

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            row = result.fetchone()

        assert row is not None, "La consulta SELECT 1 no devolvió ninguna fila"
        assert row[0] == 1, f"Se esperaba 1, se obtuvo: {row[0]}"
    finally:
        # Liberar todas las conexiones del pool explícitamente
        await engine.dispose()


@pytest.mark.integration
async def test_database_version_is_postgres_16():
    """
    Verifica que el servidor PostgreSQL es versión 16.x.
    Confirma que la imagen postgres:16 está corriendo correctamente.
    """
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_size=1,
        max_overflow=0,
    )

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version()"))
            row = result.fetchone()

        assert row is not None
        version_string: str = row[0]
        assert "PostgreSQL 16" in version_string, (
            f"Se esperaba PostgreSQL 16, versión detectada: {version_string}"
        )
    finally:
        await engine.dispose()
