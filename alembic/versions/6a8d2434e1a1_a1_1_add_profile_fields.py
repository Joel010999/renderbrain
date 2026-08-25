"""A1.1 — Add profile fields to missions and canonical_signals

Revision ID: 6a8d2434e1a1
Revises: 7a8e3dea2dec
Create Date: 2026-08-25

Changes:
    missions:
        - target_type VARCHAR(50) NOT NULL DEFAULT 'post'
        - observation_scope VARCHAR(50) NULL
        - story_interval_seconds INTEGER NULL

    canonical_signals:
        - content_type VARCHAR(50) NULL
        - native_id VARCHAR(255) NULL
        - source_account_username VARCHAR(255) NULL
        - source_account_name VARCHAR(255) NULL
        - source_account_id VARCHAR(100) NULL

Compatibilidad:
    Todas las columnas nuevas en missions tienen DEFAULT 'post' / NULL,
    garantizando compatibilidad total con filas existentes.
    Las columnas nuevas en canonical_signals son nullable: las señales
    existentes no requieren migración de datos.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a8d2434e1a1"
down_revision: Union[str, Sequence[str], None] = "7a8e3dea2dec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Agrega columnas de perfil a missions y canonical_signals."""

    # ------------------------------------------------------------------
    # missions — target_type, observation_scope, story_interval_seconds
    # ------------------------------------------------------------------
    op.add_column(
        "missions",
        sa.Column(
            "target_type",
            sa.String(length=50),
            nullable=False,
            server_default="post",
        ),
    )
    op.add_column(
        "missions",
        sa.Column("observation_scope", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "missions",
        sa.Column("story_interval_seconds", sa.Integer(), nullable=True),
    )

    # ------------------------------------------------------------------
    # canonical_signals — content_type, native_id, provenance
    # ------------------------------------------------------------------
    op.add_column(
        "canonical_signals",
        sa.Column("content_type", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "canonical_signals",
        sa.Column("native_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "canonical_signals",
        sa.Column("source_account_username", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "canonical_signals",
        sa.Column("source_account_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "canonical_signals",
        sa.Column("source_account_id", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    """Elimina las columnas de perfil agregadas en A1.1."""

    # canonical_signals
    op.drop_column("canonical_signals", "source_account_id")
    op.drop_column("canonical_signals", "source_account_name")
    op.drop_column("canonical_signals", "source_account_username")
    op.drop_column("canonical_signals", "native_id")
    op.drop_column("canonical_signals", "content_type")

    # missions
    op.drop_column("missions", "story_interval_seconds")
    op.drop_column("missions", "observation_scope")
    op.drop_column("missions", "target_type")
