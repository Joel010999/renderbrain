"""
runtime/contracts/knowledge.py

Contratos del Knowledge Core — S3.1
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator


class Evidence(BaseModel):
    """
    Representa una evidencia atómica derivada de un CanonicalSignal.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    mission_id: UUID
    canonical_signal_id: UUID
    content: str
    confidence: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class Insight(BaseModel):
    """
    Representa un insight derivado lógicamente de una Evidence.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    mission_id: UUID
    evidence_id: UUID
    content: str
    confidence: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class KnowledgeTransaction(BaseModel):
    """
    Transacción atómica de conocimiento.
    Contiene exactamente una Evidence y un Insight (S3.1).
    """
    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    mission_id: UUID
    action: str = Field(default="CREATE_KNOWLEDGE")
    evidence: Evidence
    insight: Insight
    producer: str
    reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()

    @model_validator(mode="after")
    def validate_coherence(self) -> "KnowledgeTransaction":
        if self.mission_id != self.evidence.mission_id:
            raise ValueError("El mission_id de la Evidence no coincide con el de la KnowledgeTransaction.")
        if self.mission_id != self.insight.mission_id:
            raise ValueError("El mission_id del Insight no coincide con el de la KnowledgeTransaction.")
        if self.insight.evidence_id != self.evidence.id:
            raise ValueError("El evidence_id del Insight debe apuntar exactamente a la Evidence provista.")
        return self


class InsightSummary(BaseModel):
    """
    Representación resumida de un Insight para usar como contexto.
    Omitimos IDs en el prompt, pero los mantenemos en el modelo para trazabilidad.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    content: str
    confidence: float | None = None
    created_at: datetime

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class PatternSummary(BaseModel):
    """
    Representación resumida de un Pattern para usar como contexto.
    """
    model_config = ConfigDict(populate_by_name=True)
    
    id: UUID
    content: str
    confidence: float | None = None
    support_count: int
    created_at: datetime

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class OpportunitySummary(BaseModel):
    """
    Representación resumida de una Oportunidad para usar como contexto.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    content: str
    confidence: float | None = None
    created_at: datetime

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class MissionIntelligenceView(BaseModel):
    """
    Vista estructurada que integra Insights, Patterns y Opportunities recientes,
    permitiendo que los componentes cognitivos tengan conciencia histórica completa.
    Reemplaza lógicamente a KnowledgeContext.
    """
    model_config = ConfigDict(populate_by_name=True)

    mission_id: UUID
    insights: list[InsightSummary] = Field(default_factory=list)
    patterns: list[PatternSummary] = Field(default_factory=list)
    opportunities: list[OpportunitySummary] = Field(default_factory=list)


# Alias por retrocompatibilidad temporal si algún lugar usaba KnowledgeContext fuertemente acoplado.
# (Preferimos reemplazar todo uso en S5.4)
KnowledgeContext = MissionIntelligenceView


class Pattern(BaseModel):
    """
    Representa una recurrencia significativa detectada a través de múltiples Insights.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    mission_id: UUID
    content: str
    confidence: float | None = None
    support_count: int = Field(ge=2)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


class Opportunity(BaseModel):
    """
    Representa una posibilidad concreta y accionable derivada del conocimiento acumulado de una Mission.
    Debe relacionarse con al menos 1 Pattern.
    """
    model_config = ConfigDict(populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    mission_id: UUID
    content: str
    confidence: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_serializer("created_at")
    def serialize_datetime(self, value: datetime) -> str:
        return value.isoformat()


