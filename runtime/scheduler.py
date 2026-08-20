"""
runtime/scheduler.py

Entrypoint del runtime de ejecución planificada de misiones (Scheduler).
"""

import asyncio
import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

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
    """Wrapper para ejecutar la misión desde el job de APScheduler."""
    # Instanciamos dependencias locales para evitar compartir
    # sesiones u objetos I/O entre múltiples corrutinas del scheduler.
    from uuid import UUID
    
    redis = get_redis_client()
    event_bus = RedisEventBus(redis_client=redis, stream=settings.EVENT_BUS_STREAM)
    orchestrator = MissionSchedulerOrchestrator(sensor_factory, event_bus)

    try:
        async with async_session() as session:
            repo = MissionRepository(session)
            mission = await repo.get_by_id(UUID(mission_id))

        if mission and mission.enabled:
            await orchestrator.execute_mission(mission)
        elif mission and not mission.enabled:
            logger.info("Mission is disabled, skipping execution", extra={"mission_id": mission_id})
        else:
            logger.warning("Mission not found in DB during execution", extra={"mission_id": mission_id})
    except Exception as e:
        logger.error(
            "Unhandled error in job wrapper",
            extra={"mission_id": mission_id, "error": str(e)},
            exc_info=True,
        )
    finally:
        await redis.aclose()


async def sync_missions(scheduler: AsyncIOScheduler, sensor_factory: SensorFactory) -> None:
    """
    Sincroniza las Misiones habilitadas en PostgreSQL con APScheduler.
    
    Estrategia mínima (S4.4):
    - Lee misiones habilitadas.
    - Agrega/actualiza el job usando str(mission.id) como ID.
    - Mantiene el intervalo de la misión en el schedule.
    - Quita jobs que ya no están habilitados.
    """
    try:
        async with async_session() as session:
            repo = MissionRepository(session)
            enabled_missions = await repo.list_enabled()

        active_mission_ids = {str(m.id) for m in enabled_missions}
        
        # Eliminar jobs huérfanos (misiones deshabilitadas o eliminadas)
        existing_jobs = [job.id for job in scheduler.get_jobs()]
        for job_id in existing_jobs:
            # Los jobs de sincronización interna no se tocan, 
            # solo verificamos los que son UUIDs (misiones).
            if job_id != "sync_missions" and job_id not in active_mission_ids:
                scheduler.remove_job(job_id)
                logger.info("Removed disabled mission job", extra={"mission_id": job_id})

        # Agregar/Actualizar misiones habilitadas
        for mission in enabled_missions:
            job_id = str(mission.id)
            job = scheduler.get_job(job_id)

            if job:
                # Si existe pero cambió el intervalo, se reprograma
                if job.trigger.interval.total_seconds() != mission.interval_seconds:
                    scheduler.reschedule_job(
                        job_id,
                        trigger="interval",
                        seconds=mission.interval_seconds,
                    )
                    logger.info("Rescheduled mission job", extra={"mission_id": job_id, "interval": mission.interval_seconds})
            else:
                scheduler.add_job(
                    _execute_job,
                    trigger="interval",
                    seconds=mission.interval_seconds,
                    id=job_id,
                    args=[job_id, sensor_factory],
                )
                logger.info(
                    "Added new mission job",
                    extra={"mission_id": job_id, "interval": mission.interval_seconds},
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

    # Job de sincronización periódica (cada 60 segundos por ejemplo)
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
            pass # Windows compatibility

    try:
        # Loop productivo, espera señal de cierre
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down Scheduler Runtime (KeyboardInterrupt)...")
    finally:
        scheduler.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
