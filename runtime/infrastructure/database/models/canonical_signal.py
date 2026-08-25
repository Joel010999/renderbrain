"""
runtime/infrastructure/database/models/canonical_signal.py

Modelo ORM SQLAlchemy para la tabla `canonical_signals`.

A1.1 — Extensión:
    - content_type: VARCHAR NULL — "reel" | "post" | "story"
    - native_id: VARCHAR NULL — ID nativo de Instagram
    - source_account_username: VARCHAR NULL — provenance de la cuenta
    - source_account_name: VARCHAR NULL — nombre de display de la cuenta
    - source_account_id: VARCHAR NULL — ID numérico de la cuenta

Principios de diseño:
- Hereda de Base (DeclarativeBase) definida en session.py.
- Separación estricta de capas: este modelo NO es el contrato Pydantic.
- UUIDs como tipos nativos (SQLAlchemy Uuid, nativo en PG como uuid).
- Timestamps con timezone=True para garantizar consistencia UTC en PG.
- metrics usa JSON (estándar SQL); PostgreSQL lo almacena como jsonb.
- No hay columnas updated_at ni deleted_at: la señal canónica es inmutable.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from runtime.infrastructure.database.session import Base


class CanonicalSignalModel(Base):
    """
    Representación ORM de la tabla canonical_signals.

    Inmutable por diseño: el repository no expone update() ni delete().
    """

    __tablename__ = "canonical_signals"

    # ------------------------------------------------------------------
    # Identidad
    # ------------------------------------------------------------------
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Contexto y trazabilidad
    # ------------------------------------------------------------------
    mission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    source_event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    sensor: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Clasificación de contenido — A1.1
    # ------------------------------------------------------------------
    content_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    native_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Provenance de cuenta — A1.1
    # ------------------------------------------------------------------
    source_account_username: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    source_account_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    source_account_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Contenido normalizado
    # ------------------------------------------------------------------
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    author: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    language: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Métricas — JSONB nativo de PostgreSQL
    # ------------------------------------------------------------------
    metrics: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Temporalidad — timezone-aware
    # ------------------------------------------------------------------
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    normalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
