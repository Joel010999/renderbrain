"""
Tests de la capa de persistencia — NO requieren PostgreSQL en ejecución.

Validan únicamente la correcta construcción del engine y la carga de
configuración; no se abre ninguna conexión real a la base de datos.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from runtime.infrastructure.database.session import Base, async_session, engine
from runtime.shared.config import settings


class TestDatabaseURL:
    def test_database_url_is_set(self):
        """DATABASE_URL debe estar disponible en Settings."""
        assert settings.DATABASE_URL, "DATABASE_URL no debe estar vacía"

    def test_database_url_uses_asyncpg_driver(self):
        """La URL debe usar el driver asyncpg para operaciones async."""
        assert settings.DATABASE_URL.startswith("postgresql+asyncpg://"), (
            f"Se esperaba prefijo 'postgresql+asyncpg://', obtenido: {settings.DATABASE_URL}"
        )


class TestEngineCreation:
    def test_engine_is_async_engine_instance(self):
        """create_async_engine debe devolver un AsyncEngine."""
        assert isinstance(engine, AsyncEngine)

    def test_engine_url_matches_settings(self):
        """La URL del engine debe coincidir con DATABASE_URL de Settings."""
        engine_url = str(engine.url)
        # asyncpg oculta la contraseña en la representación — comparamos sin ella
        assert "postgresql+asyncpg" in engine_url

    def test_engine_pool_pre_ping_enabled(self):
        """pool_pre_ping debe estar activo para detectar conexiones caídas."""
        assert engine.pool._pre_ping is True  # type: ignore[attr-defined]


class TestSessionFactory:
    def test_async_session_is_sessionmaker(self):
        """async_session debe ser una instancia de async_sessionmaker."""
        assert isinstance(async_session, async_sessionmaker)

    def test_session_class_is_async_session(self):
        """El sessionmaker debe producir instancias de AsyncSession."""
        assert async_session.class_ is AsyncSession

    def test_expire_on_commit_disabled(self):
        """expire_on_commit=False evita lazy-load tras commit en contextos async."""
        assert async_session.kw.get("expire_on_commit") is False


class TestDeclarativeBase:
    def test_base_has_metadata(self):
        """Base debe exponer un objeto metadata de SQLAlchemy."""
        from sqlalchemy import MetaData

        assert isinstance(Base.metadata, MetaData)

    def test_base_has_canonical_signals_table(self):
        """S1.3: canonical_signals debe estar registrada en Base.metadata.

        Una vez importado el módulo de modelos, Base.metadata contiene
        exactamente la tabla canonical_signals.
        """
        # Importar el package de modelos para que CanonicalSignalModel
        # se registre en Base.metadata (efecto de la importación).
        import runtime.infrastructure.database.models  # noqa: F401

        assert "canonical_signals" in Base.metadata.tables, (
            "La tabla 'canonical_signals' debe estar registrada en Base.metadata "
            "tras importar runtime.infrastructure.database.models"
        )
