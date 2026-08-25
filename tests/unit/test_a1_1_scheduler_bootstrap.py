"""
tests/unit/test_a1_1_scheduler_bootstrap.py

Tests para validar el comportamiento de bootstrap y reinicio de
Misiones de Perfil en el Scheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from runtime.contracts.mission import Mission
from runtime.scheduler import sync_missions


class DummySensorFactory:
    pass


@pytest.fixture
async def scheduler():
    sched = AsyncIOScheduler()
    sched.start()
    yield sched
    sched.shutdown(wait=False)


@pytest.fixture
def sensor_factory():
    return DummySensorFactory()


@pytest.mark.asyncio
async def test_new_profile_mission_triggers_immediate_bootstrap(scheduler, sensor_factory):
    """1. Profile nueva -> ejecución inmediata."""
    mission = Mission(
        id=uuid4(),
        name="New Profile",
        source="instagram",
        target="whapycom",
        target_type="profile",
        interval_seconds=86400,
        story_interval_seconds=None,
        last_collected_at=None,
        enabled=True
    )
    repo_mock = AsyncMock()
    repo_mock.list_enabled.return_value = [mission]

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"):
        await sync_missions(scheduler, sensor_factory)

    job = scheduler.get_job(str(mission.id))
    assert job is not None
    assert job.next_run_time is not None

    now = datetime.now(timezone.utc)
    delta = abs((job.next_run_time - now).total_seconds())
    assert delta < 5, "Debe correr inmediatamente"


@pytest.mark.asyncio
async def test_execute_job_updates_last_collected_after_success(sensor_factory, caplog):
    """2. orchestrator success -> last_collected_at se persiste DESPUES del éxito."""
    caplog.set_level(logging.INFO)

    mission = Mission(
        id=uuid4(),
        name="To Bootstrap",
        source="instagram",
        target="whapycom",
        target_type="profile",
        interval_seconds=86400,
        last_collected_at=None,
        enabled=True
    )

    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mission

    orchestrator_mock = AsyncMock()

    redis_mock = MagicMock()
    redis_mock.aclose = AsyncMock()

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"), \
         patch("runtime.scheduler.MissionSchedulerOrchestrator", return_value=orchestrator_mock), \
         patch("runtime.scheduler.get_redis_client", return_value=redis_mock), \
         patch("runtime.scheduler.RedisEventBus"):

        from runtime.scheduler import _execute_job
        await _execute_job(str(mission.id), sensor_factory)

    # Verifica orden: execution primero
    orchestrator_mock.execute_mission.assert_awaited_once_with(mission)
    repo_mock.save.assert_awaited_once()
    assert mission.last_collected_at is not None
    assert "Profile bootstrap collection completed" in caplog.text


@pytest.mark.asyncio
async def test_execute_job_failure_does_not_update_last_collected(sensor_factory, caplog):
    """3. orchestrator global failure -> last_collected_at sigue None."""
    caplog.set_level(logging.ERROR)

    mission = Mission(
        id=uuid4(),
        name="Fail Bootstrap",
        source="instagram",
        target="whapycom",
        target_type="profile",
        interval_seconds=86400,
        last_collected_at=None,
        enabled=True
    )

    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mission

    orchestrator_mock = AsyncMock()
    orchestrator_mock.execute_mission.side_effect = Exception("Global Apify Failure")

    redis_mock = MagicMock()
    redis_mock.aclose = AsyncMock()

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"), \
         patch("runtime.scheduler.MissionSchedulerOrchestrator", return_value=orchestrator_mock), \
         patch("runtime.scheduler.get_redis_client", return_value=redis_mock), \
         patch("runtime.scheduler.RedisEventBus"):

        from runtime.scheduler import _execute_job
        await _execute_job(str(mission.id), sensor_factory)

    # Si falla globalmente, NO guardamos mission
    repo_mock.save.assert_not_called()
    assert mission.last_collected_at is None
    assert "Unhandled error in job wrapper" in caplog.text


@pytest.mark.asyncio
async def test_restart_after_failure_retries_bootstrap(scheduler, sensor_factory):
    """4. restart después de failure (last_collected_at=None) -> vuelve a intentar inmediato."""
    mission = Mission(
        id=uuid4(),
        name="Retry Profile",
        source="instagram",
        target="whapycom",
        target_type="profile",
        interval_seconds=86400,
        story_interval_seconds=None,
        last_collected_at=None,
        enabled=True
    )
    repo_mock = AsyncMock()
    repo_mock.list_enabled.return_value = [mission]

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"):
        await sync_missions(scheduler, sensor_factory)

    job = scheduler.get_job(str(mission.id))
    now = datetime.now(timezone.utc)
    delta = abs((job.next_run_time - now).total_seconds())
    assert delta < 5


@pytest.mark.asyncio
async def test_restart_calculates_correct_next_run(scheduler, sensor_factory):
    """
    5. last_collected_at hace 5h -> próxima corrida en ~19h.
    7. restart NO reinicia intervalo completo desde cero.
    """
    now = datetime.now(timezone.utc)
    mission = Mission(
        id=uuid4(),
        name="Active Profile",
        source="instagram",
        target="whapycom",
        target_type="profile",
        interval_seconds=86400,
        story_interval_seconds=None,
        last_collected_at=now - timedelta(hours=5),
        enabled=True
    )
    repo_mock = AsyncMock()
    repo_mock.list_enabled.return_value = [mission]

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"):
        await sync_missions(scheduler, sensor_factory)

    job = scheduler.get_job(str(mission.id))
    delta_to_run = (job.next_run_time - now).total_seconds()
    # Faltan 19h (68400 segundos)
    assert 68390 < delta_to_run <= 68400


@pytest.mark.asyncio
async def test_restart_after_long_downtime_runs_immediately(scheduler, sensor_factory):
    """6. last_collected_at hace 25h -> próxima corrida inmediata."""
    now = datetime.now(timezone.utc)
    mission = Mission(
        id=uuid4(),
        name="Long Downtime Profile",
        source="instagram",
        target="whapycom",
        target_type="profile",
        interval_seconds=86400,
        story_interval_seconds=None,
        last_collected_at=now - timedelta(hours=25),
        enabled=True
    )
    repo_mock = AsyncMock()
    repo_mock.list_enabled.return_value = [mission]

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"):
        await sync_missions(scheduler, sensor_factory)

    job = scheduler.get_job(str(mission.id))
    delta = abs((job.next_run_time - now).total_seconds())
    assert delta < 5


@pytest.mark.asyncio
async def test_legacy_post_mission_does_not_bootstrap(scheduler, sensor_factory):
    """9. Misión post legacy intacta -> default APScheduler behavior."""
    mission = Mission(
        id=uuid4(),
        name="Legacy Post",
        source="instagram",
        target="https://inst...",
        target_type="post",
        interval_seconds=3600,
        last_collected_at=None,
        enabled=True
    )
    repo_mock = AsyncMock()
    repo_mock.list_enabled.return_value = [mission]

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"):
        await sync_missions(scheduler, sensor_factory)

    job = scheduler.get_job(str(mission.id))
    now = datetime.now(timezone.utc)
    delta_to_run = (job.next_run_time - now).total_seconds()
    assert 3590 < delta_to_run <= 3600


@pytest.mark.asyncio
async def test_stories_disabled_remain_without_job(scheduler, sensor_factory):
    """10. Stories disabled siguen intactas sin crear job extra."""
    mission = Mission(
        id=uuid4(),
        name="No Stories Profile",
        source="instagram",
        target="whapycom",
        target_type="profile",
        interval_seconds=86400,
        story_interval_seconds=None,
        last_collected_at=None,
        enabled=True
    )
    repo_mock = AsyncMock()
    repo_mock.list_enabled.return_value = [mission]

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"):
        await sync_missions(scheduler, sensor_factory)

    assert scheduler.get_job(str(mission.id)) is not None
    assert scheduler.get_job(f"{mission.id}:stories") is None
