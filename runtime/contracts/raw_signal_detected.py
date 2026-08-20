"""
runtime/contracts/raw_signal_detected.py

Contrato de datos puros de captura — First Signal Flow.

Principios de diseño:
- Representa el momento exacto en que un sensor detecta una señal.
- No incluye trazabilidad de transporte (event_id, correlation_id,
  causation_id): esos campos son exclusividad del EventEnvelope que
  envuelve este payload al publicarlo en el Event Bus.
- raw_payload usa JsonValue de Pydantic para garantizar que el
  contenido es JSON-serializable sin restricciones innecesarias sobre
  su estructura interna.
- sensor y source son str extensibles — sin enum todavía, para
  mantener el contrato abierto a nuevas fuentes.

Convenciones (heredadas de EventEnvelope):
    UUID        → stdlib uuid.UUID con default_factory=uuid4
    datetime    → datetime.now(UTC), siempre timezone-aware
    Pydantic    → BaseModel + ConfigDict(populate_by_name=True)
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer


class RawSignalDetected(BaseModel):
    """
    Datos puros de captura producidos por un BaseSensor.

    Este modelo representa el qué y el cuándo de una señal detectada,
    sin contener lógica de normalización ni trazabilidad de transporte.
    El EventEnvelope es el responsable de envolver este payload cuando
    se publica en el Event Bus.
    """

    model_config = ConfigDict(populate_by_name=True)

    # ------------------------------------------------------------------
    # Origen y contexto
    # ------------------------------------------------------------------

    sensor: str = Field(
        description=(
            "Nombre del sensor que detectó la señal "
            "(ej. 'manual', 'reddit_scraper', 'twitter_api')."
        ),
    )
    source: str = Field(
        description=(
            "Plataforma o fuente concreta de donde proviene la señal "
            "(ej. 'twitter', 'reddit', 'linkedin')."
        ),
    )
    mission_id: UUID = Field(
        description="Identificador de la misión a la que pertenece esta captura.",
    )

    # ------------------------------------------------------------------
    # Temporalidad — timezone-aware en UTC
    # ------------------------------------------------------------------

    captured_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description=(
            "Timestamp del momento de captura, timezone-aware en UTC. "
            "Se autogenera si no se proporciona."
        ),
    )

    # ------------------------------------------------------------------
    # Contenido capturado
    # ------------------------------------------------------------------

    raw_payload: dict[str, JsonValue] = Field(
        description=(
            "Datos crudos de la señal en formato JSON serializable. "
            "La estructura interna depende del sensor y la fuente."
        ),
    )

    # ------------------------------------------------------------------
    # Serializadores (Pydantic v2) — mismo patrón que EventEnvelope
    # ------------------------------------------------------------------

    @field_serializer("captured_at")
    def serialize_captured_at(self, value: datetime) -> str:
        """Serializa captured_at como ISO-8601 con timezone explícito."""
        return value.isoformat()
