"""
runtime/infrastructure/database/models/mission.py

Modelos de SQLAlchemy para Missions y ProcessedSignals (S4.1).
"""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from runtime.infrastructure.database.session import Base


class MissionModel(Base):
    __tablename__ = "missions"

    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String, nullable=False)
    source = Column(String, nullable=False)
    target = Column(String, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    interval_seconds = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)


class ProcessedSignalModel(Base):
    __tablename__ = "processed_signals"

    id = Column(UUID(as_uuid=True), primary_key=True)
    mission_id = Column(UUID(as_uuid=True), ForeignKey("missions.id"), nullable=False)
    source = Column(String, nullable=False)
    fingerprint = Column(String, nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("mission_id", "source", "fingerprint", name="uq_processed_signal"),
    )
