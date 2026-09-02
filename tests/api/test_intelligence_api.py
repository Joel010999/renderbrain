import pytest
from uuid import uuid4, UUID
from datetime import datetime, timezone
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.api.main import app
from runtime.contracts.mission import Mission
from runtime.contracts.knowledge import InsightSummary, PatternSummary, OpportunitySummary
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
from runtime.infrastructure.database.models.knowledge import EvidenceModel, InsightModel, PatternModel, OpportunityModel
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel

@pytest.fixture
async def api_client(monkeypatch):
    from runtime.shared.config import settings
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_PASSWORD", SecretStr("testpass"))
    
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        auth=("admin", "testpass")
    ) as client:
        yield client


async def _create_mission(session: AsyncSession, mission_id: UUID):
    mission = Mission(
        id=mission_id,
        name="Intelligence API Mission",
        source="test",
        target="test",
        enabled=True,
        interval_seconds=60,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    repo = MissionRepository(session)
    await repo.save(mission)
    await session.commit()

@pytest.mark.integration
async def test_intelligence_empty(api_client):
    mission_id = uuid4()
    async with async_session() as session:
        await _create_mission(session, mission_id)

    response = await api_client.get(f"/api/v1/missions/{mission_id}/intelligence")
    assert response.status_code == 200
    data = response.json()
    assert data["mission_id"] == str(mission_id)
    assert data["insights"] == []
    assert data["patterns"] == []
    assert data["opportunities"] == []

@pytest.mark.integration
async def test_intelligence_not_found(api_client):
    response = await api_client.get(f"/api/v1/missions/{uuid4()}/intelligence")
    assert response.status_code == 404

@pytest.mark.integration
async def test_intelligence_populated_and_traceability(api_client):
    mission_id = uuid4()
    insight1_id = uuid4()
    insight2_id = uuid4()
    pattern_id = uuid4()
    opportunity_id = uuid4()
    canonical_id1 = uuid4()
    canonical_id2 = uuid4()

    async with async_session() as session:
        await _create_mission(session, mission_id)
        
        # Insert CanonicalSignals
        cs1 = CanonicalSignalModel(id=canonical_id1, mission_id=mission_id, source_event_id=uuid4(), source="test", sensor="test", content="{}", captured_at=datetime.now(timezone.utc), normalized_at=datetime.now(timezone.utc))
        cs2 = CanonicalSignalModel(id=canonical_id2, mission_id=mission_id, source_event_id=uuid4(), source="test", sensor="test", content="{}", captured_at=datetime.now(timezone.utc), normalized_at=datetime.now(timezone.utc))
        session.add_all([cs1, cs2])
        await session.flush()

        # Insert evidence and insights
        ev1 = EvidenceModel(id=uuid4(), mission_id=mission_id, canonical_signal_id=canonical_id1, content="E1", created_at=datetime.now(timezone.utc))
        ev2 = EvidenceModel(id=uuid4(), mission_id=mission_id, canonical_signal_id=canonical_id2, content="E2", created_at=datetime.now(timezone.utc))
        session.add_all([ev1, ev2])
        await session.flush()

        in1 = InsightModel(id=insight1_id, mission_id=mission_id, evidence_id=ev1.id, content="I1", created_at=datetime.now(timezone.utc))
        in2 = InsightModel(id=insight2_id, mission_id=mission_id, evidence_id=ev2.id, content="I2", created_at=datetime.now(timezone.utc))
        session.add_all([in1, in2])
        await session.flush()

        # Insert pattern and link insights
        pm = PatternModel(id=pattern_id, mission_id=mission_id, content="P1", support_count=2, created_at=datetime.now(timezone.utc), insights=[in1, in2])
        session.add(pm)
        await session.flush()

        # Insert opportunity and link pattern
        om = OpportunityModel(id=opportunity_id, mission_id=mission_id, content="O1", created_at=datetime.now(timezone.utc), patterns=[pm])
        session.add(om)
        await session.commit()

    # 1. Test /intelligence
    res_intel = await api_client.get(f"/api/v1/missions/{mission_id}/intelligence")
    assert res_intel.status_code == 200
    intel_data = res_intel.json()
    assert len(intel_data["insights"]) == 2
    assert len(intel_data["patterns"]) == 1
    assert len(intel_data["opportunities"]) == 1

    # 2. Test /patterns traceability
    res_patterns = await api_client.get(f"/api/v1/missions/{mission_id}/patterns")
    assert res_patterns.status_code == 200
    patterns_data = res_patterns.json()
    assert len(patterns_data) == 1
    p = patterns_data[0]
    assert p["id"] == str(pattern_id)
    assert set(p["supporting_insight_ids"]) == {str(insight1_id), str(insight2_id)}

    # 3. Test /opportunities traceability
    res_opps = await api_client.get(f"/api/v1/missions/{mission_id}/opportunities")
    assert res_opps.status_code == 200
    opps_data = res_opps.json()
    assert len(opps_data) == 1
    o = opps_data[0]
    assert o["id"] == str(opportunity_id)
    assert set(o["supporting_pattern_ids"]) == {str(pattern_id)}

@pytest.mark.integration
async def test_intelligence_mission_isolation(api_client):
    mission_A = uuid4()
    mission_B = uuid4()
    canonical_id = uuid4()
    async with async_session() as session:
        await _create_mission(session, mission_A)
        await _create_mission(session, mission_B)
        
        # Add CanonicalSignal
        cs = CanonicalSignalModel(id=canonical_id, mission_id=mission_A, source_event_id=uuid4(), source="test", sensor="test", content="{}", captured_at=datetime.now(timezone.utc), normalized_at=datetime.now(timezone.utc))
        session.add(cs)
        await session.flush()

        # Add insight to mission A
        ev = EvidenceModel(id=uuid4(), mission_id=mission_A, canonical_signal_id=canonical_id, content="E", created_at=datetime.now(timezone.utc))
        session.add(ev)
        await session.flush()
        session.add(InsightModel(id=uuid4(), mission_id=mission_A, evidence_id=ev.id, content="I", created_at=datetime.now(timezone.utc)))
        await session.commit()

    res_A = await api_client.get(f"/api/v1/missions/{mission_A}/intelligence")
    assert len(res_A.json()["insights"]) == 1

    res_B = await api_client.get(f"/api/v1/missions/{mission_B}/intelligence")
    assert len(res_B.json()["insights"]) == 0

@pytest.mark.integration
async def test_intelligence_invalid_limits(api_client):
    mission_id = uuid4()
    async with async_session() as session:
        await _create_mission(session, mission_id)

    # limit > 100 violates constraint
    response = await api_client.get(f"/api/v1/missions/{mission_id}/intelligence?insight_limit=200")
    assert response.status_code == 422


@pytest.mark.integration
async def test_retry_content_generation_endpoint(api_client):
    mission_id = uuid4()
    opportunity_id = uuid4()

    async with async_session() as session:
        await _create_mission(session, mission_id)
        
        # Insert opportunity with attempts = 3
        om = OpportunityModel(
            id=opportunity_id,
            mission_id=mission_id,
            content="O1",
            created_at=datetime.now(timezone.utc),
            content_generation_attempts=3
        )
        session.add(om)
        await session.commit()

    # Hit the endpoint
    res = await api_client.post(f"/api/v1/missions/{mission_id}/opportunities/{opportunity_id}/retry-content-generation")
    
    assert res.status_code == 200
    data = res.json()
    assert data["opportunity_id"] == str(opportunity_id)
    assert data["content_generation_attempts"] == 0
    assert data["status"] == "ready_for_retry"
    
    # Verify DB update
    async with async_session() as session:
        repo = KnowledgeCoreRepository(session)
        opp = await repo.get_opportunity_by_id(opportunity_id)
        assert opp.content_generation_attempts == 0

@pytest.mark.integration
async def test_retry_content_generation_not_found(api_client):
    mission_id = uuid4()
    opportunity_id = uuid4()

    async with async_session() as session:
        await _create_mission(session, mission_id)

    res = await api_client.post(f"/api/v1/missions/{mission_id}/opportunities/{opportunity_id}/retry-content-generation")
    assert res.status_code == 404
