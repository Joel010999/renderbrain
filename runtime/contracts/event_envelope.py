"""
runtime/contracts/event_envelope.py

Contrato genérico para todos los eventos internos de RenderBrain.

Diseño:
- Todos los campos de trazabilidad (event_id, occurred_at) se autogeneran
  en el momento de instanciar el envelope, no en la infraestructura.
- correlation_id y causation_id son opcionales porque un evento raíz
  no tiene evento causante ni correlación previa. Nunca se inventan IDs.
- schema_version permite evolucionar el contrato sin romper consumidores.
- payload es dict[str, Any] — el contenido concreto lo define el evento
  de dominio en el futuro; aquí solo garantizamos serialización JSON.

Separación de identidades:
    event_id          → UUID generado por RenderBrain (viaja en el JSON)
    Redis Entry ID    → posición técnica en el stream (gestionada por el bus)
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class EventEnvelope(BaseModel):
    """
    Sobre genérico que encapsula cualquier evento interno de RenderBrain.

    Todos los productores deben empaquetar sus eventos en este contrato
    antes de publicarlos en el Event Bus.
    """

    model_config = ConfigDict(
        # Permite construir desde dict directamente (útil en deserialización)
        populate_by_name=True,
    )

    # ------------------------------------------------------------------
    # Campos de identidad — autogenerados, inmutables tras la creación
    # ------------------------------------------------------------------

    event_id: UUID = Field(
        default_factory=uuid4,
        description="Identificador único del evento dentro de RenderBrain.",
    )
    occurred_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="Timestamp de creación del evento, timezone-aware en UTC.",
    )

    # ------------------------------------------------------------------
    # Campos de tipo y contrato
    # ------------------------------------------------------------------

    event_type: str = Field(
        description="Nombre canónico del evento (ej. 'signal.raw.detected').",
    )
    schema_version: str = Field(
        default="1.0",
        description="Versión del esquema del contrato EventEnvelope.",
    )

    # ------------------------------------------------------------------
    # Payload del evento
    # ------------------------------------------------------------------

    payload: dict[str, Any] = Field(
        description="Datos concretos del evento — debe ser JSON serializable.",
    )

    # ------------------------------------------------------------------
    # Campos de trazabilidad — opcionales en eventos raíz
    # ------------------------------------------------------------------

    correlation_id: UUID | None = Field(
        default=None,
        description=(
            "ID de correlación que agrupa eventos relacionados en un flujo. "
            "None si el evento es raíz de su propio flujo."
        ),
    )
    causation_id: UUID | None = Field(
        default=None,
        description=(
            "event_id del evento que causó directamente éste. "
            "None si el evento no fue originado por otro evento."
        ),
    )

    # ------------------------------------------------------------------
    # Serializadores custom (Pydantic v2)
    # ------------------------------------------------------------------

    @field_serializer("occurred_at")
    def serialize_occurred_at(self, value: datetime) -> str:
        """Serializa occurred_at como ISO-8601 con timezone explícito."""
        return value.isoformat()
