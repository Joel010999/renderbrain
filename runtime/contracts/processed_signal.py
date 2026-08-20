"""
runtime/contracts/processed_signal.py

Contrato para el registro de huellas (fingerprints) de señales ya procesadas.
Asegura idempotencia y deduplicación exacta a nivel operacional.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class ProcessedSignal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    mission_id: UUID
    source: str
    fingerprint: str
    processed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("source", "fingerprint")
    @classmethod
    def validate_non_empty_strings(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"El campo '{info.field_name}' no puede estar vacío.")
        return v.strip()
