"""
runtime/scheduler.py

Entrypoint del runtime de ejecución planificada de misiones (Scheduler).

A1.1 — Extensión para misiones de perfil:
    - sync_missions() detecta target_type='profile' y registra DOS jobs por misión:
        1. Job principal ({mission_id}):         cada interval_seconds (86400)
           → recolecta Posts + Reels
        2. Job de stories ({mission_id}:stories): cada story_interval_seconds (21600)
           → recolecta Solo Stories (fallo soft — no afecta al job principal)
    - Para target_type='post' (legado): un único job, sin cambios.
    - Ambos jobs comparten el mismo mission_id → los eventos acumulan bajo
      la misma misión (Agente 2 intacto).
"""

import asyncio
import logging
from typing import Any
from uuid import UUID

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from runtime.contracts.mission import Mission
from runtime.engines.sensors.factory import DefaultSensorFactory, SensorFactory
from runtime.events.bus import RedisEventBus
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.infrastructure.redis.client import get_redis_client
from runtime.orchestration.mission_scheduler import MissionSchedulerOrchestrator
from runtime.shared.config import settings
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


async def _execute_job(mission_id: str, sensor_factory: SensorFactory) -> None:
    """Wrapper para ejecutar la misión principal (Posts + Reels) desde APScheduler."""
    redis = get_redis_client()
    event_bus = RedisEventBus(redis_client=redis, stream=settings.EVENT_BUS_STREAM)
    orchestrator = MissionSchedulerOrchestrator(sensor_factory, event_bus)

    try:
        async with async_session() as session:
            repo = MissionRepository(session)
            mission = await repo.get_by_id(UUID(mission_id))

            if mission and mission.enabled:
                is_bootstrap = (mission.target_type == "profile" and mission.last_collected_at is None)
                if is_bootstrap:
                    logger.info(
                        "Profile bootstrap collection started",
                        extra={"mission_id": mission_id, "target": mission.target}
                    )
                elif mission.target_type == "profile":
                    logger.info(
                        "Profile scheduled collection started",
                        extra={"mission_id": mission_id, "target": mission.target}
                    )

                from datetime import datetime, timezone
                mission.last_collected_at = datetime.now(timezone.utc)
                await repo.save(mission)
                await session.commit()
            else:
                if mission and not mission.enabled:
                    logger.info("Mission is disabled, skipping execution", extra={"mission_id": mission_id})
                else:
                    logger.warning("Mission not found in DB during execution", extra={"mission_id": mission_id})
                return

        # Execute orchestrator outside the session lock
        await orchestrator.execute_mission(mission)

        if is_bootstrap:
            logger.info(
                "Profile bootstrap collection completed",
                extra={"mission_id": mission_id, "items_collected": "delegated_to_sensor"}
            )

    except Exception as e:
        logger.error(
            "Unhandled error in job wrapper",
            extra={"mission_id": mission_id, "error": str(e)},
            exc_info=True,
        )
    finally:
        await redis.aclose()


async def _execute_stories_job(mission_id: str, sensor_factory: SensorFactory) -> None:
    """
    Wrapper para ejecutar SOLO la recolección de Stories desde APScheduler.

    Llamado cada story_interval_seconds (default 21600 = 6h).
    Fallo soft — si stories no están disponibles, loguea y termina limpiamente.
    """
    redis = get_redis_client()
    event_bus = RedisEventBus(redis_client=redis, stream=settings.EVENT_BUS_STREAM)
    orchestrator = MissionSchedulerOrchestrator(sensor_factory, event_bus)

    try:
        async with async_session() as session:
            repo = MissionRepository(session)
            mission = await repo.get_by_id(UUID(mission_id))

        if mission and mission.enabled:
            await orchestrator.execute_stories(mission)
        elif mission and not mission.enabled:
            logger.info(
                "Profile stories job skipped (mission disabled)",
                extra={"mission_id": mission_id},
            )
        else:
            logger.warning(
                "Mission not found for stories job",
                extra={"mission_id": mission_id},
            )
    except Exception as e:
        logger.error(
            "Unhandled error in stories job wrapper",
            extra={"mission_id": mission_id, "error": str(e)},
            exc_info=True,
        )
    finally:
        await redis.aclose()


async def sync_missions(scheduler: AsyncIOScheduler, sensor_factory: SensorFactory) -> None:
    """
    Sincroniza las Misiones habilitadas en PostgreSQL con APScheduler.

    Para target_type='post':  un job por misión (legado).
    Para target_type='profile': dos jobs por misión:
        - Job principal ({mission_id}) → Posts + Reels cada interval_seconds
        - Job de stories ({mission_id}:stories) → Stories cada story_interval_seconds
    """
    try:
        async with async_session() as session:
            repo = MissionRepository(session)
            enabled_missions = await repo.list_enabled()

        active_mission_ids = {str(m.id) for m in enabled_missions}
        # Para profiles: también considerar los job IDs de stories
        active_job_ids: set[str] = set()
        for m in enabled_missions:
            active_job_ids.add(str(m.id))
            if m.target_type == "profile":
                active_job_ids.add(f"{m.id}:stories")

        # Eliminar jobs huérfanos (misiones deshabilitadas o eliminadas)
        existing_jobs = [job.id for job in scheduler.get_jobs()]
        for job_id in existing_jobs:
            if job_id != "sync_missions" and job_id not in active_job_ids:
                scheduler.remove_job(job_id)
                logger.info("Removed orphaned job", extra={"job_id": job_id})

        # Agregar/Actualizar misiones habilitadas
        for mission in enabled_missions:
            job_id = str(mission.id)
            job = scheduler.get_job(job_id)

            # A1.1: Bootstrap logic. Si es perfil y nunca recolectó, ejecutar de inmediato
            kwargs = {}
            if mission.target_type == "profile" and mission.last_collected_at is None:
                from datetime import datetime, timezone
                kwargs["next_run_time"] = datetime.now(timezone.utc)

            if job:
                # Si existe pero cambió el intervalo, reprogramar
                if job.trigger.interval.total_seconds() != mission.interval_seconds:
                    scheduler.reschedule_job(
                        job_id,
                        trigger="interval",
                        seconds=mission.interval_seconds,
                    )
                    logger.info(
                        "Rescheduled mission job",
                        extra={"mission_id": job_id, "interval": mission.interval_seconds},
                    )
            else:
                scheduler.add_job(
                    _execute_job,
                    trigger="interval",
                    seconds=mission.interval_seconds,
                    id=job_id,
                    args=[job_id, sensor_factory],
                    **kwargs
                )
                logger.info(
                    "Added new mission job",
                    extra={
                        "mission_id": job_id,
                        "target_type": mission.target_type,
                        "interval": mission.interval_seconds,
                        "is_bootstrap": "next_run_time" in kwargs
                    },
                )

            # --- Job de Stories (solo para perfiles con story_interval_seconds explícito) ---
            # REGLA: el job de stories SOLO se crea cuando mission.story_interval_seconds
            # está explícitamente configurado en la Mission. Esto evita crear un job
            # que falle cada 6h cuando el actor de stories no está configurado (sessionid).
            # El Collector de Posts+Reels funciona perfectamente sin stories.
            if mission.target_type == "profile" and mission.story_interval_seconds is not None:
                stories_interval = mission.story_interval_seconds
                stories_job_id = f"{mission.id}:stories"
                stories_job = scheduler.get_job(stories_job_id)

                if stories_job:
                    if stories_job.trigger.interval.total_seconds() != stories_interval:
                        scheduler.reschedule_job(
                            stories_job_id,
                            trigger="interval",
                            seconds=stories_interval,
                        )
                        logger.info(
                            "Rescheduled stories job",
                            extra={"stories_job_id": stories_job_id, "interval": stories_interval},
                        )
                else:
                    scheduler.add_job(
                        _execute_stories_job,
                        trigger="interval",
                        seconds=stories_interval,
                        id=stories_job_id,
                        args=[str(mission.id), sensor_factory],
                    )
                    logger.info(
                        "Added new stories job",
                        extra={
                            "stories_job_id": stories_job_id,
                            "interval": stories_interval,
                        },
                    )

    except Exception as e:
        logger.error("Failed to sync missions", exc_info=True, extra={"error": str(e)})


async def main() -> None:
    """Arranca el scheduler runtime y mantiene el proceso vivo."""
    from runtime.shared.config import validate_scheduler_settings
    validate_scheduler_settings()

    logger.info("Starting Scheduler Runtime...")

    scheduler = AsyncIOScheduler()
    sensor_factory = DefaultSensorFactory()

    # Sincronización inicial
    await sync_missions(scheduler, sensor_factory)

    # Job de sincronización periódica (cada 60 segundos)
    scheduler.add_job(
        sync_missions,
        trigger="interval",
        seconds=60,
        id="sync_missions",
        args=[scheduler, sensor_factory],
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started")

    stop_event = asyncio.Event()

    def handle_shutdown(signame):
        logger.info(f"Received {signame}, shutting down Scheduler Runtime...")
        stop_event.set()

    import signal
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda sig=sig: handle_shutdown(sig.name))
        except NotImplementedError:
            pass  # Windows compatibility

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down Scheduler Runtime (KeyboardInterrupt)...")
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
