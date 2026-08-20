"""
runtime/contracts/canonical_signal.py

Contrato de señal normalizada — First Signal Flow.

Principios de diseño:
- Representa el resultado de normalizar un RawSignalDetected.
- CanonicalSignalData contiene exclusivamente los datos producto de la
  normalización, manteniendo al normalizador agnóstico a la trazabilidad de transporte.
- CanonicalSignal hereda de CanonicalSignalData e incluye source_event_id,
  que es el vínculo persistente hacia el event_id del EventEnvelope original.
- metrics es opcional y deliberadamente simple: dict[str, float | int]
  para métricas numéricas básicas (likes, reach, views...) sin crear
  modelos específicos por plataforma en esta etapa.

Convenciones (heredadas de EventEnvelope):
    UUID        → stdlib uuid.UUID con default_factory=uuid4
    datetime    → datetime.now(UTC), siempre timezone-aware
    Pydantic    → BaseModel + ConfigDict(populate_by_name=True)
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class CanonicalSignalData(BaseModel):
    """
    Datos normalizados producidos por un BaseNormalizer.

    Contiene todos los datos de dominio pero NO la trazabilidad de transporte
    (source_event_id).
    """

    model_config = ConfigDict(populate_by_name=True)

    # ------------------------------------------------------------------
    # Identidad propia
    # ------------------------------------------------------------------

    id: UUID = Field(
        default_factory=uuid4,
        description="Identificador único de esta señal canónica.",
    )

    # ------------------------------------------------------------------
    # Contexto
    # ------------------------------------------------------------------

    mission_id: UUID = Field(
        description="Misión a la que pertenece esta señal (mismo que en RawSignalDetected).",
    )
    source: str = Field(
        description="Plataforma de origen (heredado del RawSignalDetected).",
    )
    sensor: str = Field(
        description="Sensor que originó la captura (heredado del RawSignalDetected).",
    )

    # ------------------------------------------------------------------
    # Contenido normalizado
    # ------------------------------------------------------------------

    content: str = Field(
        description="Texto normalizado y limpio extraído del raw_payload.",
    )
    author: str | None = Field(
        default=None,
        description="Autor del contenido original, si está disponible.",
    )
    language: str | None = Field(
        default=None,
        description="Código ISO 639-1 del idioma detectado (ej. 'es', 'en'). Opcional.",
    )

    # ------------------------------------------------------------------
    # Métricas básicas — numéricas, sin modelos por plataforma
    # ------------------------------------------------------------------

    metrics: dict[str, float | int] | None = Field(
        default=None,
        description=(
            "Métricas de engagement numéricas básicas "
            "(ej. {'likes': 12, 'reach': 400, 'comments': 3}). "
            "Valores solo float o int — sin modelos específicos por plataforma."
        ),
    )

    # ------------------------------------------------------------------
    # Temporalidad — timezone-aware en UTC
    # ------------------------------------------------------------------

    captured_at: datetime = Field(
        description=(
            "Timestamp original de captura, heredado del RawSignalDetected. "
            "Timezone-aware en UTC."
        ),
    )
    normalized_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description=(
            "Timestamp del momento de normalización, timezone-aware en UTC. "
            "Se autogenera al instanciar."
        ),
    )

    # ------------------------------------------------------------------
    # Serializadores (Pydantic v2) — mismo patrón que EventEnvelope
    # ------------------------------------------------------------------

    @field_serializer("captured_at", "normalized_at")
    def serialize_datetime(self, value: datetime) -> str:
        """Serializa datetimes como ISO-8601 con timezone explícito."""
        return value.isoformat()


class CanonicalSignal(CanonicalSignalData):
    """
    Señal normalizada final.

    Extiende CanonicalSignalData con la trazabilidad de transporte requerida
    (source_event_id).
    """

    source_event_id: UUID = Field(
        description=(
            "event_id del EventEnvelope que transportó el RawSignalDetected original. "
            "Vínculo persistente de trazabilidad entre la señal canónica y su origen."
        ),
    )
