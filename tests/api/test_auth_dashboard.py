import pytest
from httpx import AsyncClient, ASGITransport
from pydantic import SecretStr

from runtime.api.main import app

@pytest.fixture
async def unauth_client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as client:
        yield client

@pytest.fixture
async def auth_client(monkeypatch):
    from runtime.shared.config import settings
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_PASSWORD", SecretStr("testpass"))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        auth=("admin", "testpass")
    ) as client:
        yield client

@pytest.fixture
async def wrong_auth_client(monkeypatch):
    from runtime.shared.config import settings
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_PASSWORD", SecretStr("testpass"))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        auth=("admin", "wrong")
    ) as client:
        yield client

@pytest.mark.integration
async def test_public_routes(unauth_client):
    response = await unauth_client.get("/health")
    assert response.status_code == 200

    response = await unauth_client.get("/ready")
    assert response.status_code in [200, 503]

@pytest.mark.integration
async def test_protected_routes_unauth_all(unauth_client):
    routes = [
        "/api/v1/missions",
        "/api/v1/missions/00000000-0000-0000-0000-000000000000",
        "/api/v1/missions/00000000-0000-0000-0000-000000000000/intelligence",
        "/api/v1/missions/00000000-0000-0000-0000-000000000000/insights",
        "/api/v1/missions/00000000-0000-0000-0000-000000000000/patterns",
        "/api/v1/missions/00000000-0000-0000-0000-000000000000/opportunities",
        "/dashboard",
        "/dashboard/missions/00000000-0000-0000-0000-000000000000"
    ]
    for route in routes:
        response = await unauth_client.get(route)
        assert response.status_code == 401

    post_routes = [
        "/api/v1/missions",
        "/api/v1/missions/00000000-0000-0000-0000-000000000000/enable",
        "/api/v1/missions/00000000-0000-0000-0000-000000000000/disable"
    ]
    for route in post_routes:
        response = await unauth_client.post(route, json={})
        assert response.status_code == 401

    patch_routes = [
        "/api/v1/missions/00000000-0000-0000-0000-000000000000"
    ]
    for route in patch_routes:
        response = await unauth_client.patch(route, json={})
        assert response.status_code == 401

@pytest.mark.integration
async def test_protected_routes_wrong_auth(wrong_auth_client):
    response = await wrong_auth_client.get("/dashboard")
    assert response.status_code == 401

@pytest.mark.integration
async def test_protected_routes_auth(auth_client):
    response = await auth_client.get("/api/v1/missions")
    assert response.status_code == 200

    response = await auth_client.get("/dashboard")
    assert response.status_code == 200
    assert "RenderBrain Dashboard" in response.text

@pytest.mark.integration
async def test_dashboard_mutations(auth_client):
    # C. Create mission
    payload = {
        "name": "Dashboard Mission",
        "source": "instagram",
        "target": "dash_target",
        "interval_seconds": 3600,
        "enabled": True
    }
    res = await auth_client.post("/api/v1/missions", json=payload)
    assert res.status_code == 201
    mission_id = res.json()["id"]

    # A. Dashboard shows mission
    res_dash = await auth_client.get("/dashboard")
    assert res_dash.status_code == 200
    assert "Dashboard Mission" in res_dash.text

    # B. Mission detail shows Insight/Pattern/Opportunity
    # We just check the page loads properly (no intelligence yet but it renders)
    res_detail = await auth_client.get(f"/dashboard/missions/{mission_id}")
    assert res_detail.status_code == 200
    assert "Dashboard Mission" in res_detail.text
    assert "No patterns detected yet." in res_detail.text

    # D. Edit
    edit_payload = {"name": "Edited Dash Mission"}
    res_edit = await auth_client.patch(f"/api/v1/missions/{mission_id}", json=edit_payload)
    assert res_edit.status_code == 200
    res_detail2 = await auth_client.get(f"/dashboard/missions/{mission_id}")
    assert "Edited Dash Mission" in res_detail2.text

    # E. Disable
    res_disable = await auth_client.post(f"/api/v1/missions/{mission_id}/disable")
    assert res_disable.status_code == 200
    assert res_disable.json()["enabled"] is False

    # F. Enable
    res_enable = await auth_client.post(f"/api/v1/missions/{mission_id}/enable")
    assert res_enable.status_code == 200
    assert res_enable.json()["enabled"] is True

    # G. Unsupported source -> 422 -> Mission not created
    bad_payload = {
        "name": "Bad Mission",
        "source": "invalid",
        "target": "none",
        "interval_seconds": 60,
        "enabled": True
    }
    res_bad = await auth_client.post("/api/v1/missions", json=bad_payload)
    assert res_bad.status_code == 422
    
    res_dash2 = await auth_client.get("/dashboard")
    assert "Bad Mission" not in res_dash2.text

@pytest.mark.integration
async def test_dashboard_with_intelligence(auth_client):
    from runtime.infrastructure.database import async_session
    from runtime.infrastructure.database.models.knowledge import EvidenceModel, InsightModel, PatternModel, OpportunityModel
    from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
    from runtime.contracts.mission import Mission
    from runtime.infrastructure.database.repositories.mission import MissionRepository
    from uuid import uuid4
    from datetime import datetime, timezone

    mission_id = uuid4()
    mission = Mission(id=mission_id, name="Intelligence Mission", source="instagram", target="target", interval_seconds=60, enabled=True)
    
    async with async_session() as session:
        repo = MissionRepository(session)
        await repo.save(mission)

        # Inject fake intelligence
        signal = CanonicalSignalModel(id=uuid4(), mission_id=mission_id, source_event_id=uuid4(), source="src", sensor="sen", content="c", captured_at=datetime.now(timezone.utc), normalized_at=datetime.now(timezone.utc))
        session.add(signal)
        evidence = EvidenceModel(id=uuid4(), mission_id=mission_id, canonical_signal_id=signal.id, content="ev", created_at=datetime.now(timezone.utc))
        session.add(evidence)
        insight = InsightModel(id=uuid4(), mission_id=mission_id, evidence_id=evidence.id, content="Unique Insight 123", confidence=0.9, created_at=datetime.now(timezone.utc))
        session.add(insight)
        pattern = PatternModel(id=uuid4(), mission_id=mission_id, content="Unique Pattern 123", confidence=0.8, support_count=2, created_at=datetime.now(timezone.utc))
        pattern.insights.append(insight)
        session.add(pattern)
        opp = OpportunityModel(id=uuid4(), mission_id=mission_id, content="Unique Opportunity 123", confidence=0.9, created_at=datetime.now(timezone.utc))
        opp.patterns.append(pattern)
        session.add(opp)
        await session.commit()

    res = await auth_client.get(f"/dashboard/missions/{mission_id}")
    assert res.status_code == 200
    assert "Unique Insight 123" in res.text
    assert "Unique Pattern 123" in res.text
    assert "Unique Opportunity 123" in res.text
