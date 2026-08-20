"""
Tests para RawSignalDetected.
"""

from datetime import UTC
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from runtime.contracts import RawSignalDetected


def test_raw_signal_detected_success():
    """Prueba la creación exitosa con campos mínimos."""
    mission_id = uuid4()
    signal = RawSignalDetected(
        sensor="test_sensor",
        source="test_source",
        mission_id=mission_id,
        raw_payload={"key": "value", "number": 42},
    )

    assert signal.sensor == "test_sensor"
    assert signal.source == "test_source"
    assert signal.mission_id == mission_id
    assert signal.raw_payload == {"key": "value", "number": 42}
    
    # Defaults
    assert signal.captured_at is not None
    assert signal.captured_at.tzinfo == UTC


def test_raw_signal_detected_invalid_mission_id():
    """Valida que mission_id debe ser un UUID válido."""
    with pytest.raises(ValidationError):
        RawSignalDetected(
            sensor="test_sensor",
            source="test_source",
            mission_id="invalid-uuid",
            raw_payload={"key": "value"},
        )


def test_raw_signal_detected_serialization():
    """Verifica la serialización JSON del datetime."""
    signal = RawSignalDetected(
        sensor="test",
        source="test",
        mission_id=uuid4(),
        raw_payload={"key": "value"},
    )
    dumped = signal.model_dump()
    json_dump = signal.model_dump_json()

    assert "captured_at" in dumped
    assert "captured_at" in json_dump
    assert "Z" in json_dump or "+00:00" in json_dump
