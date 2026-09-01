"""
runtime/infrastructure/database/models/content_brief.py

Modelo ORM para la tabla content_briefs — Agent 3.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from runtime.infrastructure.database.session import Base


class ContentBriefModel(Base):
    """
    Representación ORM de la tabla content_briefs.

    Idempotencia garantizada por UNIQUE(opportunity_id):
        Un máximo de 1 ContentBrief activo por Opportunity.
        INSERT ... ON CONFLICT DO NOTHING en el repositorio.

    Trazabilidad:
        mission_id     → Mission
        opportunity_id → Opportunity (Agent 2) → Patterns → Insights → Signals

    sections almacenado como JSONB:
        [{"order": 1, "title": "...", "content": "..."}]
    """

    __tablename__ = "content_briefs"

    __table_args__ = (
        UniqueConstraint("opportunity_id", name="uq_content_brief_opportunity"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False
    )
    mission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    opportunity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True
    )

    content_format: Mapped[str] = mapped_column(String(50), nullable=False)
    objective: Mapped[str] = mapped_column(String(50), nullable=False)
    target_audience: Mapped[str] = mapped_column(Text, nullable=False)
    angle: Mapped[str] = mapped_column(String(50), nullable=False)
    brand_service_alignment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    core_message: Mapped[str] = mapped_column(Text, nullable=False)
    hook: Mapped[str] = mapped_column(Text, nullable=False)
    sections: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cta: Mapped[str] = mapped_column(Text, nullable=False)
    visual_direction: Mapped[str] = mapped_column(Text, nullable=False)
    source_reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
