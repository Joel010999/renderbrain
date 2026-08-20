from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from runtime.contracts.knowledge import InsightSummary, PatternSummary, OpportunitySummary, MissionIntelligenceView
from runtime.contracts.mission import Mission


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
    enabled: bool = True
    interval_seconds: int

    @field_validator("name", "source", "target")
    @classmethod
    def validate_non_empty_strings(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"El campo '{info.field_name}' no puede estar vacío.")
        return v.strip()

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("interval_seconds debe ser mayor a 0.")
        return v


class MissionUpdateRequest(BaseModel):
    name: str | None = None
    source: str | None = None
    target: str | None = None
    enabled: bool | None = None
    interval_seconds: int | None = None

    @field_validator("name", "source", "target")
    @classmethod
    def validate_non_empty_strings(cls, v: str | None, info) -> str | None:
        if v is not None and (not v or not v.strip()):
            raise ValueError(f"El campo '{info.field_name}' no puede estar vacío si es provisto.")
        return v.strip() if v is not None else None

    @field_validator("interval_seconds")
    @classmethod
    def validate_interval(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("interval_seconds debe ser mayor a 0.")
        return v
