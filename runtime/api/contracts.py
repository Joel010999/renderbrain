from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from runtime.contracts.knowledge import InsightSummary, PatternSummary, OpportunitySummary, MissionIntelligenceView
from runtime.contracts.content_brief import ContentBrief
from runtime.contracts.mission import (
    Mission,
    OBSERVATION_SCOPES,
    TARGET_TYPES,
    DEFAULT_PROFILE_INTERVAL_SECONDS,
    normalize_instagram_username,
)


class PatternResponse(PatternSummary):
    """
    Extensión de PatternSummary que incluye los IDs de los insights que lo soportan.
    Esto permite la trazabilidad hacia abajo.
    """
    supporting_insight_ids: list[UUID] = Field(default_factory=list)


class OpportunityResponse(OpportunitySummary):
    """
    Extensión de OpportunitySummary que incluye los IDs de los patterns que la soportan.
    Esto permite la trazabilidad hacia abajo.
    """
    supporting_pattern_ids: list[UUID] = Field(default_factory=list)


class MissionCreateRequest(BaseModel):
    name: str
    source: str
    target: str
    target_type: str = Field(
        default="post",
        description="'post' para URL individual, 'profile' para perfil de Instagram.",
    )
    observation_scope: str | None = Field(
        default=None,
        description=(
            "Propósito de observación para perfiles: "
            "'competitor' | 'inspiration' | 'market' | 'client' | 'reference'."
        ),
    )
    enabled: bool = True
    interval_seconds: int | None = Field(
        default=None,
        description=(
            "Intervalo de ejecución en segundos. "
            "Si es None y target_type='profile', se usa el default de 86400 (24h)."
        ),
    )
    story_interval_seconds: int | None = Field(
        default=None,
        description=(
            "Intervalo para recolección de Stories en segundos. "
            "Default 21600 (6h) si no se especifica y target_type='profile'."
        ),
    )

    @field_validator("name", "source", "target")
    @classmethod
    def validate_non_empty_strings(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"El campo '{info.field_name}' no puede estar vacío.")
        return v.strip()

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in TARGET_TYPES:
            raise ValueError(
                f"target_type inválido: '{v}'. Valores permitidos: {sorted(TARGET_TYPES)}."
            )
        return v

    @field_validator("observation_scope")
    @classmethod
    def validate_observation_scope(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in OBSERVATION_SCOPES:
            raise ValueError(
                f"observation_scope inválido: '{v}'. "
                f"Valores permitidos: {sorted(OBSERVATION_SCOPES)}."
            )
        return v

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("interval_seconds debe ser mayor a 0.")
        return v

    @field_validator("story_interval_seconds")
    @classmethod
    def validate_story_interval(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("story_interval_seconds debe ser mayor a 0.")
        return v

    @model_validator(mode="after")
    def apply_profile_defaults_and_normalize(self) -> "MissionCreateRequest":
        """
        - Si target_type='profile' e interval_seconds no provisto → default 86400.
        - Normaliza el target para target_type='profile' (quita @, URL → username).
        """
        if self.target_type == "profile":
            # Aplicar default de intervalo si no se especificó
            if self.interval_seconds is None:
                self.interval_seconds = DEFAULT_PROFILE_INTERVAL_SECONDS
            # Normalizar el target a username limpio
            try:
                self.target = normalize_instagram_username(self.target)
            except ValueError as exc:
                raise ValueError(
                    f"El campo 'target' no es un perfil de Instagram válido: {exc}"
                ) from exc
        else:
            # Para post: interval_seconds es requerido
            if self.interval_seconds is None:
                raise ValueError(
                    "interval_seconds es requerido para target_type='post'."
                )
        return self


class MissionUpdateRequest(BaseModel):
    name: str | None = None
    source: str | None = None
    target: str | None = None
    target_type: str | None = None
    observation_scope: str | None = None
    enabled: bool | None = None
    interval_seconds: int | None = None
    story_interval_seconds: int | None = None

    @field_validator("name", "source", "target")
    @classmethod
    def validate_non_empty_strings(cls, v: str | None, info) -> str | None:
        if v is not None and (not v or not v.strip()):
            raise ValueError(f"El campo '{info.field_name}' no puede estar vacío si es provisto.")
        return v.strip() if v is not None else None

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in TARGET_TYPES:
            raise ValueError(
                f"target_type inválido: '{v}'. Valores permitidos: {sorted(TARGET_TYPES)}."
            )
        return v

    @field_validator("observation_scope")
    @classmethod
    def validate_observation_scope(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if v not in OBSERVATION_SCOPES:
            raise ValueError(
                f"observation_scope inválido: '{v}'. "
                f"Valores permitidos: {sorted(OBSERVATION_SCOPES)}."
            )
        return v

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("interval_seconds debe ser mayor a 0.")
        return v

    @field_validator("story_interval_seconds")
    @classmethod
    def validate_story_interval(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("story_interval_seconds debe ser mayor a 0.")
        return v

    @model_validator(mode="after")
    def apply_profile_normalization(self) -> "MissionUpdateRequest":
        if self.target_type == "profile" and self.target is not None:
            try:
                self.target = normalize_instagram_username(self.target)
            except ValueError as exc:
                raise ValueError(
                    f"El campo 'target' no es un perfil de Instagram válido: {exc}"
                ) from exc
        return self


# ---------------------------------------------------------------------------
# Agent 3 — ContentBrief API response contracts
# ---------------------------------------------------------------------------


class ContentBriefSectionResponse(BaseModel):
    """Sección individual del brief para la respuesta de la API."""

    order: int
    title: str | None = None
    content: str


class ContentBriefResponse(BaseModel):
    """
    Respuesta de API para un ContentBrief.

    Incluye todos los campos para trazabilidad completa:
        opportunity_id → Opportunity (Agent 2) → Patterns → Insights → Signals
    """

    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    mission_id: UUID
    opportunity_id: UUID
    content_format: str
    objective: str
    target_audience: str
    angle: str
    core_message: str
    hook: str
    sections: list[ContentBriefSectionResponse]
    cta: str
    visual_direction: str
    source_reasoning: str
    status: str
    created_at: datetime

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()

    @classmethod
    def from_brief(cls, brief: ContentBrief) -> "ContentBriefResponse":
        """Construye una ContentBriefResponse a partir de un ContentBrief de dominio."""
        return cls(
            id=brief.id,
            mission_id=brief.mission_id,
            opportunity_id=brief.opportunity_id,
            content_format=brief.content_format.value,
            objective=brief.objective.value,
            target_audience=brief.target_audience,
            angle=brief.angle.value,
            core_message=brief.core_message,
            hook=brief.hook,
            sections=[
                ContentBriefSectionResponse(
                    order=sec.order,
                    title=sec.title,
                    content=sec.content,
                )
                for sec in brief.sections
            ],
            cta=brief.cta,
            visual_direction=brief.visual_direction,
            source_reasoning=brief.source_reasoning,
            status=brief.status,
            created_at=brief.created_at,
        )
