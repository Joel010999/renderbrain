"""
runtime/contracts/content_brief.py

Contrato de dominio para el ContentBrief — Agent 3 (Content Strategist).

ContentBrief es la propuesta concreta de contenido generada a partir de una
Opportunity priorizada (Agent 2). Es el input del Agent 4 (Scriptwriter/Producer).

Flujo:
    Opportunity (Agent 2)
        → ContentStrategist.generate()
        → ContentBrief (este contrato)
        → Agent 4
"""

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ContentFormat(str, Enum):
    """Formato de contenido a producir."""

    reel = "reel"
    static_post = "static_post"
    carousel = "carousel"


class ContentObjective(str, Enum):
    """Objetivo comunicacional de la pieza."""

    awareness = "awareness"
    authority = "authority"
    education = "education"
    lead_generation = "lead_generation"
    engagement = "engagement"


class ContentAngle(str, Enum):
    """Ángulo narrativo de la pieza."""

    pain = "pain"
    contrarian = "contrarian"
    educational = "educational"
    comparison = "comparison"
    opportunity = "opportunity"
    mistake = "mistake"
    transformation = "transformation"


class BrandServiceAlignment(str, Enum):
    """Alineación con un servicio real de RenderByte."""

    crm = "crm"
    management_system = "management_system"
    stock_sales_collections = "stock_sales_collections"
    automation = "automation"
    ai = "ai"
    website = "website"
    ecommerce = "ecommerce"


# ---------------------------------------------------------------------------
# ContentBriefSection — representa una sección/slide del body/script
# ---------------------------------------------------------------------------


class ContentBriefSection(BaseModel):
    """
    Sección individual del body o script de una pieza de contenido.

    Para un reel: cada section es un bloque del guión.
    Para un carousel: cada section es un slide.
    Para un static_post: generalmente 1 sola section con el cuerpo completo.
    """

    model_config = ConfigDict(populate_by_name=True)

    order: int = Field(ge=1, description="Número de orden de la sección (1-based).")
    title: str | None = Field(
        default=None,
        description="Título opcional de la sección o slide.",
    )
    content: str = Field(
        min_length=1,
        description="Contenido de texto de esta sección.",
    )


# ---------------------------------------------------------------------------
# ContentBrief — contrato principal del Agent 3
# ---------------------------------------------------------------------------


class ContentBrief(BaseModel):
    """
    Propuesta concreta de contenido generada por el Content Strategist (Agent 3).

    Trazabilidad:
        - mission_id → Mission (contexto de la misión)
        - opportunity_id → Opportunity (Agent 2) → Patterns → Insights → Signals

    Idempotencia:
        Un máximo de 1 ContentBrief activo por opportunity_id.
        La capa de persistencia garantiza esta restricción via UNIQUE constraint.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    mission_id: UUID
    opportunity_id: UUID

    content_format: ContentFormat
    objective: ContentObjective
    target_audience: str = Field(min_length=1)
    angle: ContentAngle
    brand_service_alignment: BrandServiceAlignment

    core_message: str = Field(min_length=1)
    hook: str = Field(min_length=1, description="Línea de apertura corta y publicable.")
    sections: list[ContentBriefSection] = Field(
        min_length=1,
        description="Body / script estructurado como secciones ordenadas.",
    )
    cta: str = Field(min_length=1, description="Call to action coherente con el objetivo.")
    visual_direction: str = Field(
        min_length=1,
        description="Instrucción conceptual visual. NO diseño final ni colores exactos.",
    )
    source_reasoning: str = Field(
        min_length=1,
        description="Rationale resumida del porqué de la pieza. Sin chain-of-thought.",
    )

    status: str = Field(default="draft")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()

    @field_validator("sections")
    @classmethod
    def validate_sections_ordered(cls, v: list[ContentBriefSection]) -> list[ContentBriefSection]:
        """Las secciones deben estar ordenadas por order (1, 2, 3...)."""
        if not v:
            raise ValueError("sections no puede estar vacía.")
        return sorted(v, key=lambda s: s.order)
