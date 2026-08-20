"""
tests/integration/test_mission_scheduler.py

Tests para la sincronización de jobs y orquestación (S4.4).
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from runtime.contracts.mission import Mission
from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.events.bus import RedisEventBus
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.infrastructure.redis.client import get_redis_client
from runtime.orchestration.mission_scheduler import MissionSchedulerOrchestrator
from runtime.scheduler import sync_missions
from tests.integration.test_deduplication import _cleanup_all, _create_mission


class FakeSensor:
    def __init__(self, raw_signal: RawSignalDetected | None = None, error: Exception | None = None):
        self._signal = raw_signal
        self._error = error

    async def detect(self):
        if self._error:
            raise self._error
        return self._signal


class FakeSensorFactory:
    def __init__(self, sensor):
        self._sensor = sensor

    def build_sensor(self, mission):
        return self._sensor


@pytest.mark.integration
async def test_scheduler_sync_adds_enabled_missions_with_correct_interval():
    mission_id = uuid4()
    mission = await _create_mission(async_session, mission_id)

    scheduler = AsyncIOScheduler()
    factory = FakeSensorFactory(FakeSensor())

    try:
        scheduler.start()
        with patch.object(MissionRepository, "list_enabled", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [mission]
            await sync_missions(scheduler, factory)

        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.id == str(mission_id)
        assert job.trigger.interval.total_seconds() == 60  # interval_seconds defaults to 60 in _create_mission
    finally:
        await _cleanup_all(async_session, mission_id)



@pytest.mark.integration
async def test_scheduler_sync_twice_does_not_duplicate_jobs():
    mission_id = uuid4()
    mission = await _create_mission(async_session, mission_id)

    scheduler = AsyncIOScheduler()
    factory = FakeSensorFactory(FakeSensor())

    try:
        scheduler.start()
        with patch.object(MissionRepository, "list_enabled", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [mission]
            await sync_missions(scheduler, factory)
            await sync_missions(scheduler, factory)

        jobs = scheduler.get_jobs()
        assert len(jobs) == 1, "Syncing twice should not duplicate jobs"
    finally:
        await _cleanup_all(async_session, mission_id)



@pytest.mark.integration
async def test_scheduler_sync_removes_disabled_missions():
    mission_id = uuid4()
    mission = await _create_mission(async_session, mission_id)

    scheduler = AsyncIOScheduler()
    factory = FakeSensorFactory(FakeSensor())

    try:
        scheduler.start()
        with patch.object(MissionRepository, "list_enabled", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [mission]
            await sync_missions(scheduler, factory)
            assert len(scheduler.get_jobs()) == 1

            # Deshabilitar misión
            async with async_session() as session:
                repo = MissionRepository(session)
                m = await repo.get_by_id(mission_id)
                m.enabled = False
                await repo.save(m)
                await session.commit()

            # Resync
            mock_list.return_value = []
            await sync_missions(scheduler, factory)
            assert len(scheduler.get_jobs()) == 0, "Disabled mission job should be removed"
    finally:
        await _cleanup_all(async_session, mission_id)



@pytest.mark.integration
async def test_execute_mission_disabled_returns_none():
    redis = get_redis_client()
    mission_id = uuid4()
    mission = Mission(id=mission_id, name="disabled", source="test", target="test", enabled=False, interval_seconds=10)
    
    bus = RedisEventBus(redis, "test_stream")
    orchestrator = MissionSchedulerOrchestrator(FakeSensorFactory(FakeSensor()), bus)

    result = await orchestrator.execute_mission(mission)
    assert result is None
    await redis.aclose()


@pytest.mark.integration
async def test_execute_mission_publish_error_returns_none():
    redis = get_redis_client()
    mission_id = uuid4()
    mission = Mission(id=mission_id, name="enabled", source="test", target="test", enabled=True, interval_seconds=10)
    
    raw_signal = RawSignalDetected(sensor="fake", source="test", mission_id=mission_id, raw_payload={"data": "test", "fingerprint_id": "test_fp"})
    factory = FakeSensorFactory(FakeSensor(raw_signal=raw_signal))
    bus = RedisEventBus(redis, "test_stream")
    
    orchestrator = MissionSchedulerOrchestrator(factory, bus)

    # Mockear wrap_and_publish para que tire error
    with patch("runtime.orchestration.mission_scheduler.wrap_and_publish", new_callable=AsyncMock) as mock_publish:
        mock_publish.side_effect = Exception("Redis network error")
        
        result = await orchestrator.execute_mission(mission)
        
        assert result is None, "Debe retornar None al fallar publish"
        assert mock_publish.call_count == 1
    
    await redis.aclose()
