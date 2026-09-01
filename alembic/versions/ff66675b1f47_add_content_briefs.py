"""Add content_briefs table — Agent 3

Revision ID: ff66675b1f47
Revises: a5f8e6c7d1e9
Create Date: 2026-08-31 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "ff66675b1f47"
down_revision: Union[str, Sequence[str], None] = "a5f8e6c7d1e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create content_briefs table with idempotency constraint on opportunity_id."""
    op.create_table(
        "content_briefs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("mission_id", sa.UUID(), nullable=False),
        sa.Column("opportunity_id", sa.UUID(), nullable=False),
        sa.Column("content_format", sa.String(length=50), nullable=False),
        sa.Column("objective", sa.String(length=50), nullable=False),
        sa.Column("target_audience", sa.Text(), nullable=False),
        sa.Column("angle", sa.String(length=50), nullable=False),
        sa.Column("core_message", sa.Text(), nullable=False),
        sa.Column("hook", sa.Text(), nullable=False),
        sa.Column("sections", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cta", sa.Text(), nullable=False),
        sa.Column("visual_direction", sa.Text(), nullable=False),
        sa.Column("source_reasoning", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=50),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("opportunity_id", name="uq_content_brief_opportunity"),
    )
    op.create_index(
        "ix_content_briefs_mission_id",
        "content_briefs",
        ["mission_id"],
    )


def downgrade() -> None:
    """Drop content_briefs table."""
    op.drop_index("ix_content_briefs_mission_id", table_name="content_briefs")
    op.drop_table("content_briefs")
