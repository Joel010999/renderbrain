"""
runtime/contracts/mission.py

Contrato para la entidad Mission (Configuración Operacional pura).
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class Mission(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    name: str
    source: str
    target: str
    enabled: bool = True
    interval_seconds: int
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime | None = None

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
