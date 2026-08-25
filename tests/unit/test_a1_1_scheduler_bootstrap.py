"""
tests/unit/test_a1_1_scheduler_bootstrap.py

Tests para validar el comportamiento de bootstrap de Misiones de Perfil en el Scheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
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
    """
    Profile nueva + enabled -> scheduler.add_job usa next_run_time = now()
    para forzar un bootstrap inmediato.
    """
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
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval.total_seconds() == 86400
    
    # El job fue configurado para ejecutarse inmediatamente (next_run_time seteado al agregarlo)
    assert job.next_run_time is not None
    # Debería estar programado para "ahora" o en el pasado cercano
    now = datetime.now(timezone.utc)
    delta = abs((job.next_run_time - now).total_seconds())
    assert delta < 5, "El job debería estar programado para correr inmediatamente"


@pytest.mark.asyncio
async def test_already_bootstrapped_profile_does_not_trigger_immediate_run(scheduler, sensor_factory):
    """
    Si una misión de perfil ya tiene last_collected_at, el Scheduler NO usa next_run_time
    al crear el job, dejándolo para el siguiente ciclo normal.
    """
    mission = Mission(
        id=uuid4(),
        name="Old Profile",
        source="instagram",
        target="whapycom",
        target_type="profile",
        interval_seconds=86400,
        story_interval_seconds=None,
        last_collected_at=datetime.now(timezone.utc),
        enabled=True
    )

    repo_mock = AsyncMock()
    repo_mock.list_enabled.return_value = [mission]

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"):
        await sync_missions(scheduler, sensor_factory)

    job = scheduler.get_job(str(mission.id))
    assert job is not None
    # Como NO es bootstrap, el next_run_time será ~ interval_seconds en el futuro
    now = datetime.now(timezone.utc)
    delta_to_run = (job.next_run_time - now).total_seconds()
    assert 86390 < delta_to_run <= 86400, "Debería correr en ~24 horas, no de inmediato"


@pytest.mark.asyncio
async def test_legacy_post_mission_does_not_bootstrap(scheduler, sensor_factory):
    """
    Misión legacy (target_type='post') intacta: no ejecuta bootstrap inmediato.
    """
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
    assert job is not None
    now = datetime.now(timezone.utc)
    delta_to_run = (job.next_run_time - now).total_seconds()
    # El scheduler por defecto agenda para now + interval
    assert 3590 < delta_to_run <= 3600


@pytest.mark.asyncio
async def test_execute_job_updates_last_collected_at_and_logs(sensor_factory, caplog):
    """
    El wrapper _execute_job detecta que last_collected_at es None, loguea bootstrap,
    actualiza last_collected_at y lo persiste.
    """
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
    mission_id_str = str(mission.id)

    repo_mock = AsyncMock()
    repo_mock.get_by_id.return_value = mission

    # Simulamos el orquestador
    orchestrator_mock = AsyncMock()

    redis_mock = MagicMock()
    redis_mock.aclose = AsyncMock()

    with patch("runtime.scheduler.MissionRepository", return_value=repo_mock), \
         patch("runtime.scheduler.async_session"), \
         patch("runtime.scheduler.MissionSchedulerOrchestrator", return_value=orchestrator_mock), \
         patch("runtime.scheduler.get_redis_client", return_value=redis_mock), \
         patch("runtime.scheduler.RedisEventBus"):
        
        from runtime.scheduler import _execute_job
        await _execute_job(mission_id_str, sensor_factory)

    # El orchestrator fue llamado (delegando a Apify para publicar Posts/Reels con el mismo mission_id)
    orchestrator_mock.execute_mission.assert_awaited_once_with(mission)
    
    # Se actualizó durablemente last_collected_at
    repo_mock.save.assert_awaited_once_with(mission)
    assert mission.last_collected_at is not None

    # Logs requeridos
    assert "Profile bootstrap collection started" in caplog.text
    assert "Profile bootstrap collection completed" in caplog.text


@pytest.mark.asyncio
async def test_stories_disabled_remain_without_job(scheduler, sensor_factory):
    """Stories disabled siguen sin crear el job de stories."""
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

    # Job principal sí se crea (y hace bootstrap)
    assert scheduler.get_job(str(mission.id)) is not None
    # Job de stories NO se crea
    assert scheduler.get_job(f"{mission.id}:stories") is None
