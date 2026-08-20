"""
Tests para NormalizerEngine — S2.3
"""

import pytest
from datetime import UTC, datetime
from uuid import uuid4

from runtime.engines.normalizer import NormalizerEngine
from runtime.contracts import RawSignalDetected

@pytest.fixture
def base_raw_payload():
    return {
        "url_queried": "https://www.instagram.com/p/abc",
        "items_received": 1,
        "data": {
            "caption": "Test caption",
            "ownerUsername": "testuser",
            "timestamp": "2023-01-01T12:00:00.000Z",
            "likesCount": 100,
            "commentsCount": 10,
            "viewsCount": 500,
            "playsCount": 400
        }
    }

@pytest.fixture
def mission_id():
    return uuid4()

class TestNormalizerEngineInstagram:
    async def test_full_post(self, mission_id, base_raw_payload):
        signal = RawSignalDetected(
            sensor="instagram_apify_sensor",
            source="instagram",
            mission_id=mission_id,
            raw_payload=base_raw_payload,
            captured_at=datetime(2023, 1, 1, tzinfo=UTC)
        )
        
        engine = NormalizerEngine()
        canonical = await engine.normalize(signal)
        
        assert canonical.content == "Test caption"
        assert canonical.author == "testuser"
        assert canonical.captured_at == datetime(2023, 1, 1, 12, 0, 0, tzinfo=UTC)
        assert canonical.metrics == {
            "likes": 100,
            "comments": 10,
            "views": 500,
            "plays": 400
        }
        assert canonical.language is None
        assert canonical.source == "instagram"
        assert canonical.sensor == "instagram_apify_sensor"
        assert canonical.mission_id == mission_id

    async def test_missing_optional_fields(self, mission_id, base_raw_payload):
        base_raw_payload["data"] = {} # Empty data
        
        signal = RawSignalDetected(
            sensor="instagram_apify_sensor",
            source="instagram",
            mission_id=mission_id,
            raw_payload=base_raw_payload,
        )
        
        engine = NormalizerEngine()
        canonical = await engine.normalize(signal)
        
        assert canonical.content == ""
        assert canonical.author is None
        assert canonical.captured_at == signal.captured_at
        assert canonical.metrics is None
        
    async def test_invalid_timestamp_fallback(self, mission_id, base_raw_payload):
        base_raw_payload["data"]["timestamp"] = "invalid-date"
        
        signal = RawSignalDetected(
            sensor="instagram_apify_sensor",
            source="instagram",
            mission_id=mission_id,
            raw_payload=base_raw_payload,
            captured_at=datetime(2024, 1, 1, tzinfo=UTC)
        )
        
        engine = NormalizerEngine()
        canonical = await engine.normalize(signal)
        
        # Debe caer al fallback (signal.captured_at)
        assert canonical.captured_at == datetime(2024, 1, 1, tzinfo=UTC)

    async def test_invalid_data_raises_error(self, mission_id, base_raw_payload):
        base_raw_payload["data"] = "not-a-dict"
        
        signal = RawSignalDetected(
            sensor="instagram_apify_sensor",
            source="instagram",
            mission_id=mission_id,
            raw_payload=base_raw_payload,
        )
        
        engine = NormalizerEngine()
        with pytest.raises(ValueError, match="Invalid Instagram raw_payload"):
            await engine.normalize(signal)

    async def test_missing_data_key_raises_error(self, mission_id, base_raw_payload):
        del base_raw_payload["data"]
        
        signal = RawSignalDetected(
            sensor="instagram_apify_sensor",
            source="instagram",
            mission_id=mission_id,
            raw_payload=base_raw_payload,
        )
        
        engine = NormalizerEngine()
        with pytest.raises(ValueError, match="Invalid Instagram raw_payload"):
            await engine.normalize(signal)

    async def test_original_payload_not_mutated(self, mission_id, base_raw_payload):
        import copy
        original = copy.deepcopy(base_raw_payload)
        
        signal = RawSignalDetected(
            sensor="instagram_apify_sensor",
            source="instagram",
            mission_id=mission_id,
            raw_payload=base_raw_payload,
        )
        
        engine = NormalizerEngine()
        await engine.normalize(signal)
        
        assert base_raw_payload == original

    async def test_instagram_fallback_logic(self, mission_id):
        # Base payload using the real example
        base = {
            "url_queried": "https://www.instagram.com/p/DbcFgQ0P5eE/",
            "items_received": 1,
            "data": {
                "inputUrl": "https://www.instagram.com/p/DbcFgQ0P5eE/",
                "id": "3953058780236715908",
                "type": "Video",
                "shortCode": "DbcFgQ0P5eE",
                "url": "https://www.instagram.com/p/DbcFgQ0P5eE/",
                "commentsCount": 0,
                "firstComment": "",
                "alt": "Video by Dimitris Tech",
                "likesCount": 10,
                "timestamp": "2026-07-31T01:30:02.000Z",
                "ownerFullName": "Dimitris Tech | Software a medida",
                "ownerUsername": "dimitris.tech",
                "ownerId": "46291752290",
                "productType": "clips",
                "videoPlayCount": 873
            }
        }
        
        engine = NormalizerEngine()
        
        # A. caption presente -> prioriza caption
        payload_a = base.copy()
        payload_a["data"] = base["data"].copy()
        payload_a["data"]["caption"] = "Real caption"
        payload_a["data"]["text"] = "Real text"
        sig_a = RawSignalDetected(sensor="instagram_apify_sensor", source="instagram", mission_id=mission_id, raw_payload=payload_a)
        can_a = await engine.normalize(sig_a)
        assert can_a.content == "Real caption"
        
        # B. caption ausente + text presente -> usa text
        payload_b = base.copy()
        payload_b["data"] = base["data"].copy()
        payload_b["data"]["text"] = "Real text"
        sig_b = RawSignalDetected(sensor="instagram_apify_sensor", source="instagram", mission_id=mission_id, raw_payload=payload_b)
        can_b = await engine.normalize(sig_b)
        assert can_b.content == "Real text"

        # C. caption/text ausentes + alt presente -> usa alt
        payload_c = base.copy()
        payload_c["data"] = base["data"].copy()
        sig_c = RawSignalDetected(sensor="instagram_apify_sensor", source="instagram", mission_id=mission_id, raw_payload=payload_c)
        can_c = await engine.normalize(sig_c)
        assert can_c.content == "Video by Dimitris Tech"
        
        # D. caption/text vacíos + alt presente -> usa alt
        payload_d = base.copy()
        payload_d["data"] = base["data"].copy()
        payload_d["data"]["caption"] = "   "
        payload_d["data"]["text"] = ""
        sig_d = RawSignalDetected(sensor="instagram_apify_sensor", source="instagram", mission_id=mission_id, raw_payload=payload_d)
        can_d = await engine.normalize(sig_d)
        assert can_d.content == "Video by Dimitris Tech"
        
        # E. Verify fields (author, timestamp, metrics) with real payload
        assert can_c.author == "dimitris.tech"
        assert can_c.captured_at == datetime(2026, 7, 31, 1, 30, 2, tzinfo=UTC)
        assert can_c.metrics == {
            "likes": 10,
            "comments": 0
            # videoPlayCount is not extracted by current metric logic (only viewsCount or playsCount)
            # but we won't change the metrics extraction to maintain historic exactness unless asked.
            # wait, the logic uses playsCount. We keep it as is.
        }

class TestNormalizerEngineManual:
    async def test_manual_flow_preserved(self, mission_id):
        signal = RawSignalDetected(
            sensor="manual",
            source="manual",
            mission_id=mission_id,
            raw_payload={
                "content": "Manual content",
                "author": "manualuser",
                "metrics": {"likes": 1}
            }
        )
        
        engine = NormalizerEngine()
        canonical = await engine.normalize(signal)
        
        assert canonical.content == "Manual content"
        assert canonical.author == "manualuser"
        assert canonical.metrics == {"likes": 1}
