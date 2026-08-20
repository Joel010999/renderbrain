"""
tests/integration/test_knowledge_repository.py

Tests de integración para el KnowledgeCoreRepository (S3.1).
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.knowledge import Evidence, Insight, KnowledgeTransaction
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.knowledge import EvidenceModel
from runtime.infrastructure.database.repositories import (
    CanonicalSignalRepository,
    KnowledgeCoreRepository,
)


@pytest.mark.integration
async def test_knowledge_repository_commit_and_read():
    """
    Verifica el guardado y lectura atómica de una KnowledgeTransaction
    asegurando las relaciones con la CanonicalSignal.
    """
    mission_id = uuid4()
    source_event_id = uuid4()

    canonical = CanonicalSignal(
        mission_id=mission_id,
        source_event_id=source_event_id,
        source="test_source",
        sensor="test_sensor",
        content="Original signal text",
        captured_at=datetime.now(UTC),
    )

    async with async_session() as session:
        # 1. Persistir CanonicalSignal origen
        sig_repo = CanonicalSignalRepository(session)
        await sig_repo.save(canonical)
        await session.commit()

        # 2. Construir Transacción
        evidence = Evidence(
            mission_id=mission_id,
            canonical_signal_id=canonical.id,
            content="Evidence derived from signal",
            confidence=0.95,
        )
        insight = Insight(
            mission_id=mission_id,
            evidence_id=evidence.id,
            content="Insight derived from evidence",
            confidence=0.85,
        )
        tx = KnowledgeTransaction(
            mission_id=mission_id,
            evidence=evidence,
            insight=insight,
            producer="KnowledgeCoreTest",
            reason="Integration testing",
        )

        # 3. Commit
        know_repo = KnowledgeCoreRepository(session)
        await know_repo.commit(tx)
        await session.commit()

        # 4. Read back
        recovered = await know_repo.get_by_id(tx.id)

        assert recovered is not None
        assert recovered.id == tx.id
        assert recovered.mission_id == mission_id
        assert recovered.action == "CREATE_KNOWLEDGE"
        assert recovered.producer == "KnowledgeCoreTest"
        assert recovered.reason == "Integration testing"

        # Validar Evidence
        assert recovered.evidence.id == evidence.id
        assert recovered.evidence.canonical_signal_id == canonical.id
        assert recovered.evidence.content == "Evidence derived from signal"
        assert recovered.evidence.confidence == 0.95

        # Validar Insight
        assert recovered.insight.id == insight.id
        assert recovered.insight.evidence_id == evidence.id
        assert recovered.insight.content == "Insight derived from evidence"
        assert recovered.insight.confidence == 0.85


@pytest.mark.integration
async def test_knowledge_repository_atomicity():
    """
    Fuerza un error de Foreign Key Constraint en Evidence 
    (apuntando a un canonical_signal_id inexistente)
    y verifica que el rollback fue completo (Atomicidad).
    """
    mission_id = uuid4()

    evidence = Evidence(
        mission_id=mission_id,
        canonical_signal_id=uuid4(),  # No existe en DB
        content="Failing evidence",
    )
    insight = Insight(
        mission_id=mission_id,
        evidence_id=evidence.id,
        content="Failing insight",
    )
    tx = KnowledgeTransaction(
        mission_id=mission_id,
        evidence=evidence,
        insight=insight,
        producer="Test",
    )

    async with async_session() as session:
        know_repo = KnowledgeCoreRepository(session)
        
        # El flush interno en commit() levantará IntegrityError por la FK
        with pytest.raises(IntegrityError):
            await know_repo.commit(tx)
            
        await session.rollback()

        # Asegurar que nada quedó huérfano
        result = await session.execute(
            select(EvidenceModel).where(EvidenceModel.id == evidence.id)
        )
        assert result.scalar_one_or_none() is None
