"""
runtime/infrastructure/database/models/knowledge.py

Modelos ORM para el Knowledge Core — S3.1
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, Table, Column
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from runtime.infrastructure.database.session import Base

pattern_insights = Table(
    "pattern_insights",
    Base.metadata,
    Column("pattern_id", PG_UUID(as_uuid=True), ForeignKey("patterns.id"), primary_key=True),
    Column("insight_id", PG_UUID(as_uuid=True), ForeignKey("insights.id"), primary_key=True)
)

opportunity_patterns = Table(
    "opportunity_patterns",
    Base.metadata,
    Column("opportunity_id", PG_UUID(as_uuid=True), ForeignKey("opportunities.id"), primary_key=True),
    Column("pattern_id", PG_UUID(as_uuid=True), ForeignKey("patterns.id"), primary_key=True)
)


class EvidenceModel(Base):
    """
    Representación ORM de la tabla evidence.
    """
    __tablename__ = "evidence"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, nullable=False)
    mission_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    
    # Foreign Key hacia la señal canónica base
    canonical_signal_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("canonical_signals.id"), nullable=False
    )
    
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relaciones
    insight: Mapped["InsightModel"] = relationship("InsightModel", back_populates="evidence", uselist=False)
    knowledge_transaction: Mapped["KnowledgeTransactionModel"] = relationship(
        "KnowledgeTransactionModel", back_populates="evidence", uselist=False
    )


class InsightModel(Base):
    """
    Representación ORM de la tabla insights.
    """
    __tablename__ = "insights"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, nullable=False)
    mission_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    
    # Foreign Key hacia la evidencia base
    evidence_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("evidence.id"), nullable=False)
    
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relaciones
    evidence: Mapped["EvidenceModel"] = relationship("EvidenceModel", back_populates="insight")
    knowledge_transaction: Mapped["KnowledgeTransactionModel"] = relationship(
        "KnowledgeTransactionModel", back_populates="insight", uselist=False
    )
    patterns: Mapped[list["PatternModel"]] = relationship(
        "PatternModel", secondary=pattern_insights, back_populates="insights"
    )


class KnowledgeTransactionModel(Base):
    """
    Representación ORM de la tabla knowledge_transactions.
    Audita la inserción atómica de Evidence e Insight en el sistema.
    """
    __tablename__ = "knowledge_transactions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, nullable=False)
    mission_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    
    # Referencias directas a los objetos que persisten (sin cascade on delete)
    evidence_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("evidence.id"), nullable=False)
    insight_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("insights.id"), nullable=False)
    
    producer: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    evidence: Mapped["EvidenceModel"] = relationship("EvidenceModel", back_populates="knowledge_transaction")
    insight: Mapped["InsightModel"] = relationship("InsightModel", back_populates="knowledge_transaction")


class PatternModel(Base):
    """
    Representación ORM de la tabla patterns.
    """
    __tablename__ = "patterns"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, nullable=False)
    mission_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    support_count: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relaciones
    insights: Mapped[list["InsightModel"]] = relationship(
        "InsightModel", secondary=pattern_insights, back_populates="patterns"
    )
    opportunities: Mapped[list["OpportunityModel"]] = relationship(
        "OpportunityModel", secondary=opportunity_patterns, back_populates="patterns"
    )


class OpportunityModel(Base):
    """
    Representación ORM de la tabla opportunities.
    """
    __tablename__ = "opportunities"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, nullable=False)
    mission_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Relaciones
    patterns: Mapped[list["PatternModel"]] = relationship(
        "PatternModel", secondary=opportunity_patterns, back_populates="opportunities"
    )
