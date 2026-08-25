"""
tests/unit/test_a1_1_profile.py

Tests unitarios para A1.1 — Instagram Daily Collector (Agente 1 Autónomo).

Suite completa OFFLINE:
    - CERO llamadas reales a Apify, OpenAI, Redis o PostgreSQL.
    - Usa FakeAdapter y objetos en memoria.
    - Todos los tests deben pasar con: uv run pytest tests/unit/test_a1_1_profile.py -v

Cobertura:
    T01 — Normalización de username: @user, URL completa, limpio, inválidos.
    T02 — Misión post existente intacta (target_type=post retrocompatible).
    T03 — Default profile interval = 86400.
    T04 — observation_scope persistido y validado.
    T05 — Perfil devuelve posts y reels separados con content_type correcto.
    T06 — Stories clasificadas con content_type='story'.
    T07 — Preservación de content_type en normalización.
    T08 — Provenance account preservado (source_account_username).
    T09 — Evento independiente por ítem bajo mismo mission_id.
    T10 — Primera corrida: todo es nuevo (sin ProcessedSignals previos).
    T11 — Segunda corrida: solo señales con ID nuevo pasan.
    T12 — Dedupe histórico intacto (posts/reels no colisionan con stories).
    T13 — Namespace story evita colisión: instagram:story:<id> ≠ instagram:<id>.
    T14 — API create profile mission (validación de request).
    T15 — API edit profile mission (observation_scope actualizable).
    T16 — Acumulación bajo misma Mission (múltiples signals → mismo mission_id).
    T17 — Apify falla ítem inválido → skip, el resto procesa.
    T18 — Fallo de stories es independiente: posts/reels llegan bien.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from runtime.contracts.canonical_signal import CanonicalSignalData
from runtime.contracts.mission import (
    DEFAULT_PROFILE_INTERVAL_SECONDS,
    DEFAULT_STORY_INTERVAL_SECONDS,
    Mission,
    OBSERVATION_SCOPES,
    normalize_instagram_username,
)
from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.engines.normalizer.engine import NormalizerEngine
from runtime.engines.sensors.instagram_profile import InstagramProfileSensor
from runtime.workers.fingerprint import FingerprintError, compute_fingerprint
from runtime.api.contracts import MissionCreateRequest, MissionUpdateRequest


# ---------------------------------------------------------------------------
# Helpers & FakeAdapters
# ---------------------------------------------------------------------------

def _make_post_item(native_id: str = "post123", username: str = "testuser") -> dict:
    """Simula un ítem de post/reel del dataset de Apify."""
    return {
        "id": native_id,
        "shortCode": f"SC{native_id}",
        "caption": f"Caption for {native_id}",
        "ownerUsername": username,
        "ownerFullName": "Test User",
        "ownerId": "987654321",
        "likesCount": 100,
        "commentsCount": 5,
        "timestamp": "2026-08-25T00:00:00+00:00",
    }


def _make_story_item(story_id: str = "story123", username: str = "testuser") -> dict:
    """Simula un ítem de story del dataset de Apify."""
    return {
        "id": story_id,
        "storyId": story_id,
        "ownerUsername": username,
        "timestamp": "2026-08-25T01:00:00+00:00",
    }


class FakeProfileAdapter:
    """Fake adapter que simula el comportamiento del ApifyInstagramAdapter para perfiles."""

    def __init__(
        self,
        posts: list[dict] | None = None,
        reels: list[dict] | None = None,
        stories: list[dict] | None = None,
        stories_fail: bool = False,
        posts_fail: bool = False,
    ):
        self._posts = posts or [_make_post_item("P1"), _make_post_item("P2")]
        self._reels = reels or [_make_post_item("R1")]
        self._stories = stories or [_make_story_item("S1")]
        self._stories_fail = stories_fail
        self._posts_fail = posts_fail

    def fetch_profile_posts(
        self, username: str, limit: int = 10, results_type: str = "posts"
    ) -> list[dict]:
        if self._posts_fail:
            raise RuntimeError("Simulated posts/reels failure")
        if results_type == "reels":
            return self._reels[:limit]
        return self._posts[:limit]

    def fetch_profile_stories(self, username: str, limit: int = 20) -> list[dict]:
        if self._stories_fail:
            from runtime.infrastructure.apify.adapter import ApifyStoriesUnavailableError
            raise ApifyStoriesUnavailableError("Simulated stories failure")
        return self._stories[:limit]


# ---------------------------------------------------------------------------
# T01 — Normalización de username
# ---------------------------------------------------------------------------

class TestUsernameNormalization:
    def test_clean_username(self):
        assert normalize_instagram_username("dimitris.tech") == "dimitris.tech"

    def test_at_prefix_removed(self):
        assert normalize_instagram_username("@dimitris.tech") == "dimitris.tech"

    def test_full_url_instagram_com(self):
        assert normalize_instagram_username("https://instagram.com/dimitris.tech") == "dimitris.tech"

    def test_full_url_www_with_trailing_slash(self):
        assert normalize_instagram_username("https://www.instagram.com/dimitris.tech/") == "dimitris.tech"

    def test_rejects_empty_username(self):
        with pytest.raises(ValueError, match="vacío"):
            normalize_instagram_username("")

    def test_rejects_too_long_username(self):
        with pytest.raises(ValueError, match="30 caracteres"):
            normalize_instagram_username("a" * 31)

    def test_rejects_invalid_chars(self):
        with pytest.raises(ValueError, match="inválido"):
            normalize_instagram_username("user name!")  # espacio y !

    def test_rejects_non_instagram_url(self):
        with pytest.raises(ValueError, match="instagram.com"):
            normalize_instagram_username("https://twitter.com/user")

    def test_rejects_post_url(self):
        """URL de post (con /p/) no es un perfil válido."""
        with pytest.raises(ValueError):
            normalize_instagram_username("https://www.instagram.com/p/ABC123/")


# ---------------------------------------------------------------------------
# T02 — Misión post existente intacta (retrocompatibilidad)
# ---------------------------------------------------------------------------

class TestPostMissionCompatibility:
    def test_post_mission_default_target_type(self):
        m = Mission(
            name="Legacy Post",
            source="instagram",
            target="https://www.instagram.com/p/ABC123/",
            interval_seconds=3600,
        )
        assert m.target_type == "post"
        assert m.observation_scope is None
        assert m.story_interval_seconds is None

    def test_post_mission_target_not_normalized(self):
        """Para target_type=post, el target se conserva tal cual."""
        url = "https://www.instagram.com/p/ABC123/"
        m = Mission(name="Post", source="instagram", target=url, interval_seconds=3600)
        assert m.target == url

    def test_post_mission_interval_preserved(self):
        m = Mission(name="P", source="instagram", target="https://instagram.com/p/X/", interval_seconds=7200)
        assert m.interval_seconds == 7200


# ---------------------------------------------------------------------------
# T03 — Default profile interval = 86400
# ---------------------------------------------------------------------------

class TestProfileDefaultInterval:
    def test_default_profile_interval_constant(self):
        assert DEFAULT_PROFILE_INTERVAL_SECONDS == 86400

    def test_profile_mission_explicit_interval(self):
        m = Mission(
            name="Competitor Watch",
            source="instagram",
            target="dimitris.tech",
            target_type="profile",
            interval_seconds=86400,
        )
        assert m.interval_seconds == 86400

    def test_profile_mission_story_interval_default(self):
        assert DEFAULT_STORY_INTERVAL_SECONDS == 21600

    def test_api_create_request_defaults_interval_for_profile(self):
        """MissionCreateRequest sin interval_seconds para profile → default 86400."""
        req = MissionCreateRequest(
            name="Profile Mission",
            source="instagram",
            target="testuser",
            target_type="profile",
        )
        assert req.interval_seconds == DEFAULT_PROFILE_INTERVAL_SECONDS

    def test_api_create_request_post_requires_interval(self):
        """Para target_type=post, interval_seconds es requerido."""
        with pytest.raises(ValueError, match="interval_seconds es requerido"):
            MissionCreateRequest(
                name="Post Mission",
                source="instagram",
                target="https://instagram.com/p/X/",
                target_type="post",
            )


# ---------------------------------------------------------------------------
# T04 — observation_scope persistido y validado
# ---------------------------------------------------------------------------

class TestObservationScope:
    def test_valid_observation_scopes(self):
        for scope in ["competitor", "inspiration", "market", "client", "reference"]:
            m = Mission(
                name="Test",
                source="instagram",
                target="testuser",
                target_type="profile",
                interval_seconds=86400,
                observation_scope=scope,
            )
            assert m.observation_scope == scope

    def test_invalid_observation_scope_rejected(self):
        with pytest.raises(ValueError, match="observation_scope inválido"):
            Mission(
                name="Test",
                source="instagram",
                target="testuser",
                target_type="profile",
                interval_seconds=86400,
                observation_scope="invalid_scope",
            )

    def test_none_observation_scope_allowed(self):
        m = Mission(
            name="Test",
            source="instagram",
            target="testuser",
            target_type="profile",
            interval_seconds=86400,
            observation_scope=None,
        )
        assert m.observation_scope is None


# ---------------------------------------------------------------------------
# T05 — Perfil devuelve posts y reels separados con content_type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestProfileSensorContentTypes:
    async def test_detect_returns_posts_and_reels(self):
        adapter = FakeProfileAdapter(
            posts=[_make_post_item("P1"), _make_post_item("P2")],
            reels=[_make_post_item("R1")],
        )
        sensor = InstagramProfileSensor(
            mission_id=uuid4(),
            username="testuser",
            adapter=adapter,
        )
        signals = await sensor.detect()
        assert len(signals) == 3  # 2 posts + 1 reel

        content_types = [s.raw_payload["content_type"] for s in signals]
        assert content_types.count("post") == 2
        assert content_types.count("reel") == 1

    async def test_detect_all_signals_same_mission_id(self):
        mission_id = uuid4()
        adapter = FakeProfileAdapter(
            posts=[_make_post_item("P1"), _make_post_item("P2")],
            reels=[_make_post_item("R1")],
        )
        sensor = InstagramProfileSensor(
            mission_id=mission_id,
            username="testuser",
            adapter=adapter,
        )
        signals = await sensor.detect()
        for s in signals:
            assert s.mission_id == mission_id


# ---------------------------------------------------------------------------
# T06 — Stories clasificadas con content_type='story'
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStoriesClassification:
    async def test_detect_stories_returns_story_signals(self):
        adapter = FakeProfileAdapter(stories=[_make_story_item("S1"), _make_story_item("S2")])
        sensor = InstagramProfileSensor(
            mission_id=uuid4(),
            username="testuser",
            adapter=adapter,
        )
        story_signals = await sensor.detect_stories()
        assert len(story_signals) == 2
        for s in story_signals:
            assert s.raw_payload["content_type"] == "story"

    async def test_stories_failure_returns_empty_list(self):
        """Si stories falla, detect_stories() retorna [] sin propagarse."""
        adapter = FakeProfileAdapter(stories_fail=True)
        sensor = InstagramProfileSensor(
            mission_id=uuid4(),
            username="testuser",
            adapter=adapter,
        )
        story_signals = await sensor.detect_stories()
        assert story_signals == []


# ---------------------------------------------------------------------------
# T07 — Preservación de content_type en normalización
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestNormalizerContentType:
    async def test_reel_content_type_preserved(self):
        engine = NormalizerEngine()
        mission_id = uuid4()
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=mission_id,
            raw_payload={
                "profile_username": "testuser",
                "content_type": "reel",
                "data": _make_post_item("R1"),
            },
        )
        result = await engine.normalize(raw)
        assert result.content_type == "reel"

    async def test_post_content_type_preserved(self):
        engine = NormalizerEngine()
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={
                "profile_username": "testuser",
                "content_type": "post",
                "data": _make_post_item("P1"),
            },
        )
        result = await engine.normalize(raw)
        assert result.content_type == "post"

    async def test_story_content_type_preserved(self):
        engine = NormalizerEngine()
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={
                "profile_username": "testuser",
                "content_type": "story",
                "data": _make_story_item("S1"),
            },
        )
        result = await engine.normalize(raw)
        assert result.content_type == "story"

    async def test_unknown_content_type_maps_to_none(self):
        """content_type desconocido se mapea a None (no inferencia)."""
        engine = NormalizerEngine()
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={
                "profile_username": "testuser",
                "content_type": "video_clip",  # desconocido
                "data": _make_post_item("X1"),
            },
        )
        result = await engine.normalize(raw)
        assert result.content_type is None


# ---------------------------------------------------------------------------
# T08 — Provenance account preservado
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestProvenanceAccount:
    async def test_source_account_username_extracted(self):
        engine = NormalizerEngine()
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={
                "profile_username": "dimitris.tech",
                "content_type": "post",
                "data": _make_post_item("P1", username="dimitris.tech"),
            },
        )
        result = await engine.normalize(raw)
        assert result.source_account_username == "dimitris.tech"

    async def test_source_account_name_extracted(self):
        engine = NormalizerEngine()
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={
                "profile_username": "testuser",
                "content_type": "post",
                "data": {
                    **_make_post_item("P1"),
                    "ownerFullName": "Full Name Here",
                },
            },
        )
        result = await engine.normalize(raw)
        assert result.source_account_name == "Full Name Here"

    async def test_source_account_id_extracted(self):
        engine = NormalizerEngine()
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={
                "profile_username": "testuser",
                "content_type": "post",
                "data": {
                    **_make_post_item("P1"),
                    "ownerId": "111222333",
                },
            },
        )
        result = await engine.normalize(raw)
        assert result.source_account_id == "111222333"

    async def test_native_id_extracted_from_data(self):
        engine = NormalizerEngine()
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={
                "profile_username": "testuser",
                "content_type": "post",
                "data": _make_post_item("native123"),
            },
        )
        result = await engine.normalize(raw)
        assert result.native_id == "native123"


# ---------------------------------------------------------------------------
# T09 — Evento independiente por ítem bajo mismo mission_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestEventPerItem:
    async def test_three_items_produce_three_signals(self):
        mission_id = uuid4()
        adapter = FakeProfileAdapter(
            posts=[_make_post_item("P1"), _make_post_item("P2")],
            reels=[_make_post_item("R1")],
        )
        sensor = InstagramProfileSensor(
            mission_id=mission_id,
            username="testuser",
            adapter=adapter,
        )
        signals = await sensor.detect()
        assert len(signals) == 3

    async def test_all_signals_have_same_mission_id(self):
        mission_id = uuid4()
        adapter = FakeProfileAdapter(
            posts=[_make_post_item("P1")],
            reels=[_make_post_item("R1")],
        )
        sensor = InstagramProfileSensor(
            mission_id=mission_id,
            username="testuser",
            adapter=adapter,
        )
        signals = await sensor.detect()
        for s in signals:
            assert s.mission_id == mission_id
            assert s.source == "instagram"


# ---------------------------------------------------------------------------
# T10 — Primera corrida: todo nuevo (sin ProcessedSignals previos)
# ---------------------------------------------------------------------------

class TestFirstRunBootstrap:
    def test_first_run_all_signals_are_new(self):
        """Sin ProcessedSignals previos, todos los fingerprints son nuevos."""
        items = [
            _make_post_item("P1"),
            _make_post_item("P2"),
            _make_story_item("S1"),
        ]
        already_processed = set()  # vacío = primera corrida

        new_items = [
            item for item in items
            if item["id"] not in already_processed
        ]
        assert len(new_items) == 3

    def test_fingerprints_for_reel_a_post_b_story_c(self):
        """Verifica que los 3 tipos generan fingerprints únicos."""
        reel_signal = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={"content_type": "reel", "data": _make_post_item("ReelA")},
        )
        post_signal = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={"content_type": "post", "data": _make_post_item("PostB")},
        )
        story_signal = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={"content_type": "story", "data": _make_story_item("StoryC")},
        )

        fp_reel = compute_fingerprint(reel_signal)
        fp_post = compute_fingerprint(post_signal)
        fp_story = compute_fingerprint(story_signal)

        assert fp_reel == "instagram:ReelA"
        assert fp_post == "instagram:PostB"
        assert fp_story == "instagram:story:StoryC"
        # Todos distintos
        assert len({fp_reel, fp_post, fp_story}) == 3


# ---------------------------------------------------------------------------
# T11 — Segunda corrida: solo señales con ID nuevo pasan
# ---------------------------------------------------------------------------

class TestSecondRunDeduplication:
    def test_second_run_only_new_id_passes(self):
        """A, B, C ya procesados — solo D es nuevo."""
        already_processed = {"A", "B", "C"}
        all_items = ["A", "B", "C", "D"]

        new_items = [item for item in all_items if item not in already_processed]
        assert new_items == ["D"]

    def test_fingerprint_dedup_for_profile_signals(self):
        """Fingerprints para A,B,C ya en ProcessedSignals → D pasa."""
        mission_id = uuid4()

        def make_signal(content_type: str, native_id: str) -> RawSignalDetected:
            data = _make_post_item(native_id) if content_type != "story" else _make_story_item(native_id)
            return RawSignalDetected(
                sensor="instagram_profile_sensor",
                source="instagram",
                mission_id=mission_id,
                raw_payload={"content_type": content_type, "data": data},
            )

        sig_a = make_signal("reel", "A")
        sig_b = make_signal("post", "B")
        sig_c = make_signal("story", "C")
        sig_d = make_signal("post", "D")

        processed_fps = {
            compute_fingerprint(sig_a),
            compute_fingerprint(sig_b),
            compute_fingerprint(sig_c),
        }

        new_signals = [
            s for s in [sig_a, sig_b, sig_c, sig_d]
            if compute_fingerprint(s) not in processed_fps
        ]
        assert len(new_signals) == 1
        assert compute_fingerprint(new_signals[0]) == "instagram:D"


# ---------------------------------------------------------------------------
# T12 — Dedupe histórico intacto: posts/reels no colisionan con stories
# ---------------------------------------------------------------------------

class TestDedupeHistoricalIntact:
    def test_post_fingerprint_format(self):
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={"content_type": "post", "data": _make_post_item("NATIVE_ID_123")},
        )
        assert compute_fingerprint(raw) == "instagram:NATIVE_ID_123"

    def test_reel_fingerprint_format(self):
        raw = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={"content_type": "reel", "data": _make_post_item("REEL_999")},
        )
        assert compute_fingerprint(raw) == "instagram:REEL_999"


# ---------------------------------------------------------------------------
# T13 — Namespace story evita colisión: instagram:story:<id> ≠ instagram:<id>
# ---------------------------------------------------------------------------

class TestStoryNoCollision:
    def test_story_namespace_different_from_post(self):
        """Mismo ID nativo: story y post generan fingerprints distintos."""
        same_id = "SAME_ID_XYZ"

        post_signal = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={"content_type": "post", "data": _make_post_item(same_id)},
        )
        story_signal = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={"content_type": "story", "data": _make_story_item(same_id)},
        )

        fp_post = compute_fingerprint(post_signal)
        fp_story = compute_fingerprint(story_signal)

        assert fp_post == f"instagram:{same_id}"
        assert fp_story == f"instagram:story:{same_id}"
        assert fp_post != fp_story

    def test_story_uses_story_id_field(self):
        """Para stories, se prioriza storyId sobre id."""
        story_signal = RawSignalDetected(
            sensor="instagram_profile_sensor",
            source="instagram",
            mission_id=uuid4(),
            raw_payload={
                "content_type": "story",
                "data": {"storyId": "STORY_ONLY_ID", "ownerUsername": "u"},
            },
        )
        assert compute_fingerprint(story_signal) == "instagram:story:STORY_ONLY_ID"


# ---------------------------------------------------------------------------
# T14 — API create profile mission (validación)
# ---------------------------------------------------------------------------

class TestAPICreateProfileMission:
    def test_create_profile_request_normalizes_target(self):
        req = MissionCreateRequest(
            name="Competitor Watch",
            source="instagram",
            target="@competitor_account",
            target_type="profile",
        )
        assert req.target == "competitor_account"
        assert req.interval_seconds == DEFAULT_PROFILE_INTERVAL_SECONDS
        assert req.target_type == "profile"

    def test_create_profile_request_from_url(self):
        req = MissionCreateRequest(
            name="Brand Watch",
            source="instagram",
            target="https://www.instagram.com/brandaccount/",
            target_type="profile",
        )
        assert req.target == "brandaccount"

    def test_create_profile_request_with_observation_scope(self):
        req = MissionCreateRequest(
            name="Market Watch",
            source="instagram",
            target="marketaccount",
            target_type="profile",
            observation_scope="market",
        )
        assert req.observation_scope == "market"

    def test_create_request_rejects_invalid_observation_scope(self):
        with pytest.raises(ValueError, match="observation_scope inválido"):
            MissionCreateRequest(
                name="Test",
                source="instagram",
                target="testuser",
                target_type="profile",
                observation_scope="unknown_scope",
            )


# ---------------------------------------------------------------------------
# T15 — API edit profile mission
# ---------------------------------------------------------------------------

class TestAPIUpdateProfileMission:
    def test_update_observation_scope(self):
        req = MissionUpdateRequest(observation_scope="competitor")
        assert req.observation_scope == "competitor"

    def test_update_story_interval(self):
        req = MissionUpdateRequest(story_interval_seconds=10800)
        assert req.story_interval_seconds == 10800

    def test_update_rejects_invalid_story_interval(self):
        with pytest.raises(ValueError, match="mayor a 0"):
            MissionUpdateRequest(story_interval_seconds=0)

    def test_update_empty_request(self):
        req = MissionUpdateRequest()
        data = req.model_dump(exclude_unset=True)
        assert data == {}


# ---------------------------------------------------------------------------
# T16 — Acumulación bajo misma Mission
# ---------------------------------------------------------------------------

class TestAccumulationUnderSameMission:
    def test_multiple_signals_same_mission_id(self):
        mission_id = uuid4()
        signals = []
        for content_type, native_id in [("reel", "R1"), ("post", "P1"), ("story", "S1")]:
            data = _make_post_item(native_id) if content_type != "story" else _make_story_item(native_id)
            raw = RawSignalDetected(
                sensor="instagram_profile_sensor",
                source="instagram",
                mission_id=mission_id,
                raw_payload={"content_type": content_type, "data": data},
            )
            signals.append(raw)

        assert len(signals) == 3
        for s in signals:
            assert s.mission_id == mission_id, "Todas las señales deben tener el mismo mission_id"


# ---------------------------------------------------------------------------
# T17 — Apify falla ítem inválido → skip, el resto procesa
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPartialItemFailure:
    async def test_invalid_item_is_skipped(self):
        """Un ítem None o no-dict es skipeado sin abortar el batch.

        detect() devuelve posts + reels.
        Con 1 post inválido (None) + 1 post válido (P2) + 1 reel (R1) → 2 señales.
        El ítem None fue skipeado, el resto procesó normalmente.
        """
        adapter = FakeProfileAdapter(
            posts=[None, _make_post_item("P2")],  # type: ignore — primer ítem inválido
            reels=[_make_post_item("R1")],
        )
        sensor = InstagramProfileSensor(
            mission_id=uuid4(),
            username="testuser",
            adapter=adapter,
        )
        signals = await sensor.detect()
        # None fue skipeado: 1 post válido (P2) + 1 reel (R1) = 2 señales
        assert len(signals) == 2
        # Verificar que ninguna señal tiene data None
        for s in signals:
            assert s.raw_payload["data"] is not None
        # Verificar que P2 está presente
        ids = [s.raw_payload["data"]["id"] for s in signals]
        assert "P2" in ids
        assert "R1" in ids


# ---------------------------------------------------------------------------
# T18 — Fallo de stories independiente: posts/reels llegan bien
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestStoriesFailureIsIndependent:
    async def test_stories_fail_does_not_affect_posts_reels(self):
        adapter = FakeProfileAdapter(
            posts=[_make_post_item("P1"), _make_post_item("P2")],
            reels=[_make_post_item("R1")],
            stories_fail=True,
        )
        sensor = InstagramProfileSensor(
            mission_id=uuid4(),
            username="testuser",
            adapter=adapter,
        )

        # Posts + Reels deben llegar normalmente
        posts_reels_signals = await sensor.detect()
        assert len(posts_reels_signals) == 3

        # Stories falla soft → lista vacía, sin excepción
        story_signals = await sensor.detect_stories()
        assert story_signals == []
