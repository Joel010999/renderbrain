"""
Tests para CanonicalSignal.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from runtime.contracts import CanonicalSignal, CanonicalSignalData


def test_canonical_signal_success():
    """Prueba la creación exitosa con campos mínimos."""
    mission_id = uuid4()
    source_event_id = uuid4()
    captured_at = datetime.now(UTC)

    data = CanonicalSignalData(
        mission_id=mission_id,
        source="twitter",
        sensor="twitter_api",
        content="This is a test signal",
        captured_at=captured_at,
    )

    signal = CanonicalSignal(
        **data.model_dump(),
        source_event_id=source_event_id,
    )

    assert isinstance(signal.id, UUID)
    assert signal.mission_id == mission_id
    assert signal.source_event_id == source_event_id
    assert signal.source == "twitter"
    assert signal.sensor == "twitter_api"
    assert signal.content == "This is a test signal"
    assert signal.captured_at == captured_at
    
    # Defaults
    assert signal.author is None
    assert signal.language is None
    assert signal.metrics is None
    assert signal.normalized_at is not None
    assert signal.normalized_at.tzinfo == UTC
    
    # Verificamos que CanonicalSignalData funcione
    assert data.id == signal.id
    assert not hasattr(data, "source_event_id")


def test_canonical_signal_optional_fields():
    """Verifica que los campos opcionales se asignan correctamente."""
    signal = CanonicalSignal(
        mission_id=uuid4(),
        source_event_id=uuid4(),
        source="test",
        sensor="test",
        content="test content",
        author="John Doe",
        language="en",
        metrics={"likes": 10, "reach": 100},
        captured_at=datetime.now(UTC),
    )

    assert signal.author == "John Doe"
    assert signal.language == "en"
    assert signal.metrics == {"likes": 10, "reach": 100}


def test_canonical_signal_invalid_metrics():
    """Valida que metrics no acepta tipos no numéricos."""
    with pytest.raises(ValidationError):
        CanonicalSignal(
            mission_id=uuid4(),
            source_event_id=uuid4(),
            source="test",
            sensor="test",
            content="test",
            captured_at=datetime.now(UTC),
            metrics={"likes": "many"},  # Invalido, debe ser float o int
        )


def test_canonical_signal_serialization():
    """Verifica la serialización JSON del datetime."""
    signal = CanonicalSignal(
        mission_id=uuid4(),
        source_event_id=uuid4(),
        source="test",
        sensor="test",
        content="test",
        captured_at=datetime.now(UTC),
    )
    json_dump = signal.model_dump_json()

    assert "captured_at" in json_dump
    assert "normalized_at" in json_dump
