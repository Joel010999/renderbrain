"""
tests/contracts/test_knowledge.py

Tests unitarios para los contratos del Knowledge Core (S3.1).
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from runtime.contracts.knowledge import Evidence, Insight, KnowledgeTransaction


def test_knowledge_transaction_success():
    """Valida la creación correcta de una transacción coherente."""
    mission_id = uuid4()
    sig_id = uuid4()

    evidence = Evidence(
        mission_id=mission_id,
        canonical_signal_id=sig_id,
        content="Evidence content",
        confidence=0.9,
    )
    insight = Insight(
        mission_id=mission_id,
        evidence_id=evidence.id,
        content="Insight content",
        confidence=0.8,
    )

    tx = KnowledgeTransaction(
        mission_id=mission_id,
        evidence=evidence,
        insight=insight,
        producer="TestProducer",
        reason="Test reason",
    )

    assert tx.mission_id == mission_id
    assert tx.evidence.content == "Evidence content"
    assert tx.insight.content == "Insight content"
    assert tx.producer == "TestProducer"
    assert tx.action == "CREATE_KNOWLEDGE"
    assert tx.created_at is not None


def test_transaction_mission_id_mismatch_evidence():
    """Valida que falle si el mission_id de la Evidence es diferente al de la Transacción."""
    mission_id = uuid4()
    evidence = Evidence(
        mission_id=uuid4(), canonical_signal_id=uuid4(), content="Ev"
    )
    insight = Insight(
        mission_id=mission_id, evidence_id=evidence.id, content="In"
    )

    with pytest.raises(
        ValidationError, match="El mission_id de la Evidence no coincide"
    ):
        KnowledgeTransaction(
            mission_id=mission_id,
            evidence=evidence,
            insight=insight,
            producer="test",
        )


def test_transaction_mission_id_mismatch_insight():
    """Valida que falle si el mission_id del Insight es diferente al de la Transacción."""
    mission_id = uuid4()
    evidence = Evidence(
        mission_id=mission_id, canonical_signal_id=uuid4(), content="Ev"
    )
    insight = Insight(mission_id=uuid4(), evidence_id=evidence.id, content="In")

    with pytest.raises(
        ValidationError, match="El mission_id del Insight no coincide"
    ):
        KnowledgeTransaction(
            mission_id=mission_id,
            evidence=evidence,
            insight=insight,
            producer="test",
        )


def test_transaction_evidence_id_mismatch():
    """Valida que falle si el Insight apunta a otra Evidence."""
    mission_id = uuid4()
    evidence = Evidence(
        mission_id=mission_id, canonical_signal_id=uuid4(), content="Ev"
    )
    insight = Insight(mission_id=mission_id, evidence_id=uuid4(), content="In")

    with pytest.raises(
        ValidationError,
        match="El evidence_id del Insight debe apuntar exactamente",
    ):
        KnowledgeTransaction(
            mission_id=mission_id,
            evidence=evidence,
            insight=insight,
            producer="test",
        )
