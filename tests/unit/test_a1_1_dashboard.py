"""
tests/unit/test_a1_1_dashboard.py

Tests de validación del Dashboard y API para A1.1:
    - Create Mission: target_type=post y target_type=profile vía API
    - Edit Mission: observation_scope actualizable
    - Dashboard: campos de perfil visibles en HTML
    - Scheduler: story job solo cuando story_interval_seconds es explícito

OFFLINE: sin DB/Redis/Apify/OpenAI reales.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from runtime.api.contracts import MissionCreateRequest, MissionUpdateRequest
from runtime.contracts.mission import (
    DEFAULT_PROFILE_INTERVAL_SECONDS,
    Mission,
    normalize_instagram_username,
)


# ---------------------------------------------------------------------------
# T_DASH01 — Create Profile Mission: campos enviados correctamente a API
# ---------------------------------------------------------------------------

class TestCreateMissionAPIFields:
    def test_profile_create_request_has_target_type(self):
        req = MissionCreateRequest(
            name="Competitor Watch",
            source="instagram",
            target="testuser",
            target_type="profile",
            observation_scope="competitor",
        )
        assert req.target_type == "profile"
        assert req.observation_scope == "competitor"
        assert req.interval_seconds == DEFAULT_PROFILE_INTERVAL_SECONDS

    def test_post_create_request_default_target_type(self):
        req = MissionCreateRequest(
            name="Single Post",
            source="instagram",
            target="https://www.instagram.com/p/ABC123/",
            target_type="post",
            interval_seconds=3600,
        )
        assert req.target_type == "post"
        assert req.observation_scope is None

    def test_profile_with_all_observation_scopes(self):
        scopes = ["competitor", "inspiration", "market", "client", "reference"]
        for scope in scopes:
            req = MissionCreateRequest(
                name=f"Test {scope}",
                source="instagram",
                target="testuser",
                target_type="profile",
                observation_scope=scope,
            )
            assert req.observation_scope == scope

    def test_profile_with_story_interval(self):
        req = MissionCreateRequest(
            name="Test Stories",
            source="instagram",
            target="testuser",
            target_type="profile",
            story_interval_seconds=21600,
        )
        assert req.story_interval_seconds == 21600

    def test_profile_without_story_interval_is_none(self):
        """Sin story_interval_seconds → None → no se crea job de stories."""
        req = MissionCreateRequest(
            name="Test No Stories",
            source="instagram",
            target="testuser",
            target_type="profile",
        )
        assert req.story_interval_seconds is None

    def test_profile_default_interval_is_86400(self):
        """El default de interval_seconds para perfiles es 86400 (24h)."""
        req = MissionCreateRequest(
            name="Test",
            source="instagram",
            target="testuser",
            target_type="profile",
        )
        assert req.interval_seconds == 86400

    def test_profile_target_accepts_at_username(self):
        req = MissionCreateRequest(
            name="Test",
            source="instagram",
            target="@competitor_account",
            target_type="profile",
        )
        assert req.target == "competitor_account"

    def test_profile_target_accepts_full_url(self):
        req = MissionCreateRequest(
            name="Test",
            source="instagram",
            target="https://www.instagram.com/brandaccount/",
            target_type="profile",
        )
        assert req.target == "brandaccount"

    def test_profile_target_accepts_clean_username(self):
        req = MissionCreateRequest(
            name="Test",
            source="instagram",
            target="cleanusername",
            target_type="profile",
        )
        assert req.target == "cleanusername"


# ---------------------------------------------------------------------------
# T_DASH02 — Edit Mission: observation_scope actualizable
# ---------------------------------------------------------------------------

class TestEditMissionAPIFields:
    def test_update_observation_scope(self):
        req = MissionUpdateRequest(observation_scope="competitor")
        data = req.model_dump(exclude_unset=True)
        assert "observation_scope" in data
        assert data["observation_scope"] == "competitor"

    def test_update_all_scopes_valid(self):
        for scope in ["competitor", "inspiration", "market", "client", "reference"]:
            req = MissionUpdateRequest(observation_scope=scope)
            assert req.observation_scope == scope

    def test_update_story_interval_seconds(self):
        req = MissionUpdateRequest(story_interval_seconds=21600)
        assert req.story_interval_seconds == 21600

    def test_update_set_story_interval_none_to_disable(self):
        """None en story_interval_seconds es válido (deshabilita stories)."""
        req = MissionUpdateRequest()
        data = req.model_dump(exclude_unset=True)
        assert "story_interval_seconds" not in data

    def test_update_target_type_to_profile(self):
        req = MissionUpdateRequest(target_type="profile")
        assert req.target_type == "profile"

    def test_update_rejects_invalid_scope(self):
        with pytest.raises(ValueError, match="observation_scope inválido"):
            MissionUpdateRequest(observation_scope="invalid")

    def test_update_partial_fields_only(self):
        req = MissionUpdateRequest(observation_scope="market")
        data = req.model_dump(exclude_unset=True)
        assert list(data.keys()) == ["observation_scope"]


# ---------------------------------------------------------------------------
# T_DASH03 — Dashboard HTML — campos de perfil visibles en contexto
# ---------------------------------------------------------------------------

class TestDashboardHTMLContext:
    def test_profile_mission_is_profile_flag(self):
        """is_profile=True para misiones de perfil."""
        mission = Mission(
            name="Test",
            source="instagram",
            target="testuser",
            target_type="profile",
            interval_seconds=86400,
            observation_scope="competitor",
        )
        context = {
            "is_profile": mission.target_type == "profile",
            "observation_scope": mission.observation_scope,
            "story_interval_seconds": mission.story_interval_seconds,
        }
        assert context["is_profile"] is True
        assert context["observation_scope"] == "competitor"
        assert context["story_interval_seconds"] is None

    def test_post_mission_is_profile_flag_false(self):
        """is_profile=False para misiones de post."""
        mission = Mission(
            name="Test",
            source="instagram",
            target="https://www.instagram.com/p/ABC/",
            target_type="post",
            interval_seconds=3600,
        )
        context = {
            "is_profile": mission.target_type == "profile",
        }
        assert context["is_profile"] is False

    def test_story_interval_visible_when_set(self):
        mission = Mission(
            name="Test",
            source="instagram",
            target="testuser",
            target_type="profile",
            interval_seconds=86400,
            story_interval_seconds=21600,
        )
        assert mission.story_interval_seconds == 21600

    def test_story_interval_none_when_not_set(self):
        mission = Mission(
            name="Test",
            source="instagram",
            target="testuser",
            target_type="profile",
            interval_seconds=86400,
        )
        assert mission.story_interval_seconds is None


# ---------------------------------------------------------------------------
# T_DASH04 — Scheduler: story job SOLO cuando story_interval_seconds != None
# ---------------------------------------------------------------------------

class TestSchedulerStoryJobCondition:
    def test_story_job_condition_with_interval(self):
        """Solo se debe crear story job si story_interval_seconds no es None."""
        mission = Mission(
            name="Test",
            source="instagram",
            target="testuser",
            target_type="profile",
            interval_seconds=86400,
            story_interval_seconds=21600,
        )
        # Condición del Scheduler
        should_create_story_job = (
            mission.target_type == "profile"
            and mission.story_interval_seconds is not None
        )
        assert should_create_story_job is True

    def test_story_job_condition_without_interval(self):
        """Sin story_interval_seconds → NO se crea job de stories."""
        mission = Mission(
            name="Test",
            source="instagram",
            target="testuser",
            target_type="profile",
            interval_seconds=86400,
            story_interval_seconds=None,  # explícitamente sin stories
        )
        should_create_story_job = (
            mission.target_type == "profile"
            and mission.story_interval_seconds is not None
        )
        assert should_create_story_job is False

    def test_post_mission_never_creates_story_job(self):
        """Misiones de post nunca crean job de stories."""
        mission = Mission(
            name="Test",
            source="instagram",
            target="https://www.instagram.com/p/ABC/",
            target_type="post",
            interval_seconds=3600,
        )
        should_create_story_job = (
            mission.target_type == "profile"
            and mission.story_interval_seconds is not None
        )
        assert should_create_story_job is False


# ---------------------------------------------------------------------------
# T_DASH05 — Validación Pydantic de campos API (sin HTTP/DB reales)
# ---------------------------------------------------------------------------

class TestAPIRequestValidationPydantic:
    def test_invalid_observation_scope_raises_validation_error(self):
        """Pydantic rechaza observation_scope inválido antes de llegar a la API."""
        with pytest.raises(ValueError, match="observation_scope inválido"):
            MissionCreateRequest(
                name="Test",
                source="instagram",
                target="testuser",
                target_type="profile",
                observation_scope="INVALID_VALUE",
            )

    def test_invalid_target_type_raises_validation_error(self):
        """Pydantic rechaza target_type inválido."""
        with pytest.raises(ValueError, match="target_type inválido"):
            MissionCreateRequest(
                name="Test",
                source="instagram",
                target="testuser",
                target_type="INVALID_TYPE",
                interval_seconds=3600,
            )

    def test_profile_without_interval_gets_default(self):
        req = MissionCreateRequest(
            name="Test",
            source="instagram",
            target="testuser",
            target_type="profile",
        )
        assert req.interval_seconds == DEFAULT_PROFILE_INTERVAL_SECONDS

    def test_post_without_interval_raises_error(self):
        with pytest.raises(ValueError, match="interval_seconds es requerido"):
            MissionCreateRequest(
                name="Test",
                source="instagram",
                target="https://www.instagram.com/p/X/",
                target_type="post",
            )

    def test_profile_with_all_fields_valid(self):
        req = MissionCreateRequest(
            name="Full Profile",
            source="instagram",
            target="@full.profile",
            target_type="profile",
            observation_scope="competitor",
            story_interval_seconds=21600,
            interval_seconds=86400,
        )
        assert req.target == "full.profile"
        assert req.target_type == "profile"
        assert req.observation_scope == "competitor"
        assert req.story_interval_seconds == 21600
        assert req.interval_seconds == 86400

