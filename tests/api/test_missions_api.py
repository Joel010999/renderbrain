import pytest
from uuid import uuid4
from datetime import datetime, timezone
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.api.main import app
from runtime.contracts.mission import Mission
from runtime.infrastructure.database import async_session
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
        auth=("admin", "testpass")
    ) as client:
        yield client

@pytest.mark.integration
async def test_get_missions_empty(api_client):
    response = await api_client.get("/api/v1/missions")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

@pytest.mark.integration
async def test_get_missions_populated(api_client):
    mission_id = uuid4()
    mission = Mission(
        id=mission_id,
        name="API Test Mission",
        source="test_source",
        target="test_target",
        enabled=True,
        interval_seconds=60,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    
    async with async_session() as session:
        repo = MissionRepository(session)
        await repo.save(mission)
        await session.commit()

    response = await api_client.get("/api/v1/missions")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert any(m["id"] == str(mission_id) for m in data)

@pytest.mark.integration
async def test_get_mission_by_id_exists(api_client):
    mission_id = uuid4()
    mission = Mission(
        id=mission_id,
        name="API Single Mission",
        source="test_source",
        target="test_target",
        enabled=True,
        interval_seconds=60,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    
    async with async_session() as session:
        repo = MissionRepository(session)
        await repo.save(mission)
        await session.commit()

    response = await api_client.get(f"/api/v1/missions/{mission_id}")
    assert response.status_code == 200
    assert response.json()["id"] == str(mission_id)

@pytest.mark.integration
async def test_get_mission_by_id_not_found(api_client):
    response = await api_client.get(f"/api/v1/missions/{uuid4()}")
    assert response.status_code == 404

@pytest.mark.integration
async def test_create_mission_success(api_client):
    payload = {
        "name": "Test Create Mission",
        "source": "instagram",
        "target": "target_acc",
        "enabled": True,
        "interval_seconds": 120
    }
    response = await api_client.post("/api/v1/missions", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    assert data["name"] == "Test Create Mission"
    assert data["source"] == "instagram"
    assert data["target"] == "target_acc"
    assert data["enabled"] is True
    assert data["interval_seconds"] == 120

@pytest.mark.integration
async def test_create_mission_invalid_source(api_client):
    payload = {
        "name": "Test Create Mission",
        "source": "twitter",
        "target": "target_acc",
        "enabled": True,
        "interval_seconds": 120
    }
    response = await api_client.post("/api/v1/missions", json=payload)
    assert response.status_code == 422
    assert "Unsupported source" in response.json()["detail"]

@pytest.mark.integration
async def test_create_mission_rollback(api_client, monkeypatch):
    from runtime.infrastructure.database.repositories.mission import MissionRepository
    
    # Mock save to raise an Exception
    async def mock_save(*args, **kwargs):
        raise RuntimeError("Injected database failure")

    monkeypatch.setattr(MissionRepository, "save", mock_save)

    payload = {
        "name": "Test Rollback",
        "source": "instagram",
        "target": "target_acc",
        "enabled": True,
        "interval_seconds": 120
    }
    
    # Fastapi will catch the unhandled exception and return 500 Internal Server Error (or raise it in the test client)
    import pytest
    with pytest.raises(RuntimeError, match="Injected database failure"):
        await api_client.post("/api/v1/missions", json=payload)
    
    # The transaction must be rolled back, verify it wasn't saved
    # Remove the mock and check if any mission was saved
    monkeypatch.undo()
    
    # But undo() also removed the RENDERBRAIN_ADMIN_PASSWORD monkeypatch from the fixture!
    # So we must restore it manually
    from runtime.shared.config import settings
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_USERNAME", "admin")
    monkeypatch.setattr(settings, "RENDERBRAIN_ADMIN_PASSWORD", SecretStr("testpass"))

    res = await api_client.get("/api/v1/missions")
    missions = res.json()
    assert isinstance(missions, list)
    assert not any(m["name"] == "Test Rollback" for m in missions)

@pytest.mark.integration
async def test_update_mission(api_client):
    # First create
    create_payload = {
        "name": "Test Create",
        "source": "instagram",
        "target": "tgt",
        "enabled": True,
        "interval_seconds": 60
    }
    create_res = await api_client.post("/api/v1/missions", json=create_payload)
    assert create_res.status_code == 201
    mission_id = create_res.json()["id"]

    # Then update
    patch_payload = {
        "interval_seconds": 300,
        "name": "Updated Name"
    }
    patch_res = await api_client.patch(f"/api/v1/missions/{mission_id}", json=patch_payload)
    assert patch_res.status_code == 200
    patch_data = patch_res.json()
    assert patch_data["interval_seconds"] == 300
    assert patch_data["name"] == "Updated Name"
    assert patch_data["source"] == "instagram" # unchanged
    
    # Try invalid source
    bad_patch = {"source": "reddit"}
    bad_res = await api_client.patch(f"/api/v1/missions/{mission_id}", json=bad_patch)
    assert bad_res.status_code == 422

@pytest.mark.integration
async def test_enable_disable_mission(api_client):
    create_payload = {
        "name": "Toggle Mission",
        "source": "instagram",
        "target": "tgt",
        "enabled": False,
        "interval_seconds": 60
    }
    create_res = await api_client.post("/api/v1/missions", json=create_payload)
    mission_id = create_res.json()["id"]

    # Enable
    enable_res = await api_client.post(f"/api/v1/missions/{mission_id}/enable")
    assert enable_res.status_code == 200
    assert enable_res.json()["enabled"] is True

    # Disable
    disable_res = await api_client.post(f"/api/v1/missions/{mission_id}/disable")
    assert disable_res.status_code == 200
    assert disable_res.json()["enabled"] is False

@pytest.mark.integration
async def test_scheduler_sync(api_client):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from runtime.scheduler import sync_missions
    from runtime.engines.sensors.factory import DefaultSensorFactory
    
    scheduler = AsyncIOScheduler()
    sensor_factory = DefaultSensorFactory()

    # 1. Create a mission
    create_payload = {
        "name": "Sync Mission",
        "source": "instagram",
        "target": "tgt",
        "enabled": True,
        "interval_seconds": 60
    }
    res = await api_client.post("/api/v1/missions", json=create_payload)
    mission_id = res.json()["id"]

    # Sync
    await sync_missions(scheduler, sensor_factory)
    job = scheduler.get_job(mission_id)
    assert job is not None
    assert job.trigger.interval.total_seconds() == 60

    # 2. Update interval
    patch_res = await api_client.patch(f"/api/v1/missions/{mission_id}", json={"interval_seconds": 120})
    await sync_missions(scheduler, sensor_factory)
    job = scheduler.get_job(mission_id)
    assert job is not None
    assert job.trigger.interval.total_seconds() == 120

    # 3. Disable mission
    await api_client.post(f"/api/v1/missions/{mission_id}/disable")
    await sync_missions(scheduler, sensor_factory)
    job = scheduler.get_job(mission_id)
    assert job is None

    # 4. Re-enable mission
    await api_client.post(f"/api/v1/missions/{mission_id}/enable")
    await sync_missions(scheduler, sensor_factory)
    job = scheduler.get_job(mission_id)
    assert job is not None
    assert job.trigger.interval.total_seconds() == 120
