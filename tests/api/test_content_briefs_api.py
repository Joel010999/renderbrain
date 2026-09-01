"""
tests/api/test_content_briefs_api.py
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.api.main import app
from runtime.contracts.mission import Mission
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.content_brief import ContentBriefModel
from runtime.infrastructure.database.repositories.mission import MissionRepository


@pytest.fixture
async def api_client(monkeypatch):
    from runtime.shared.config import settings
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_PASSWORD", SecretStr("testpass"))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        auth=("admin", "testpass"),
    ) as client:
        yield client


async def _create_mission(session: AsyncSession, mission_id):
    mission = Mission(
        id=mission_id,
        name="Briefs API Mission",
        source="test",
        target="test",
        enabled=True,
        interval_seconds=60,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    repo = MissionRepository(session)
    await repo.save(mission)
    await session.commit()


@pytest.mark.integration
async def test_list_content_briefs(api_client):
    """Test 18: GET /api/v1/missions/{id}/content-briefs returns list."""
    mission_id = uuid4()
    brief_id = uuid4()

    async with async_session() as session:
        await _create_mission(session, mission_id)

        # Insert a fake ContentBriefModel
        brief = ContentBriefModel(
            id=brief_id,
            mission_id=mission_id,
            opportunity_id=uuid4(),
            content_format="reel",
            objective="education",
            target_audience="Devs",
            angle="pain",
            core_message="Msg",
            hook="Hook",
            sections=[{"order": 1, "title": "T", "content": "C"}],
            cta="CTA",
            visual_direction="Visual",
            source_reasoning="Reason",
            status="draft",
            created_at=datetime.now(timezone.utc),
        )
        session.add(brief)
        await session.commit()

    response = await api_client.get(f"/api/v1/missions/{mission_id}/content-briefs")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == str(brief_id)
    assert data[0]["content_format"] == "reel"
    assert len(data[0]["sections"]) == 1


@pytest.mark.integration
async def test_get_single_content_brief(api_client):
    """Test 19: GET /api/v1/content-briefs/{id} returns single brief."""
    mission_id = uuid4()
    brief_id = uuid4()

    async with async_session() as session:
        await _create_mission(session, mission_id)

        brief = ContentBriefModel(
            id=brief_id,
            mission_id=mission_id,
            opportunity_id=uuid4(),
            content_format="static_post",
            objective="awareness",
            target_audience="Devs",
            angle="pain",
            core_message="Msg",
            hook="Hook",
            sections=[{"order": 1, "title": "T", "content": "C"}],
            cta="CTA",
            visual_direction="Visual",
            source_reasoning="Reason",
            status="draft",
            created_at=datetime.now(timezone.utc),
        )
        session.add(brief)
        await session.commit()

    response = await api_client.get(f"/api/v1/content-briefs/{brief_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(brief_id)
    assert data["content_format"] == "static_post"


@pytest.mark.integration
async def test_content_briefs_not_found(api_client):
    response = await api_client.get(f"/api/v1/content-briefs/{uuid4()}")
    assert response.status_code == 404
