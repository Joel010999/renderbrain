"""
runtime/infrastructure/database/models/mission.py

Modelos de SQLAlchemy para Missions y ProcessedSignals.

A1.1 — Extensión de MissionModel:
    - target_type: VARCHAR DEFAULT 'post' — retrocompatible con misiones existentes
    - observation_scope: VARCHAR NULL — propósito de observación del perfil
    - story_interval_seconds: INTEGER NULL — intervalo dedicado para stories
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
    # A1.1 — nuevos campos de perfil
    target_type = Column(String(50), nullable=False, default="post", server_default="post")
    observation_scope = Column(String(50), nullable=True)
    story_interval_seconds = Column(Integer, nullable=True)
    # campos existentes
    enabled = Column(Boolean, nullable=False, default=True)
    interval_seconds = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=True)
    last_collected_at = Column(DateTime(timezone=True), nullable=True)


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
