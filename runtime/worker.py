"""
runtime/worker.py

Entrypoint del runtime de Worker para procesamiento de Eventos (SignalWorker).
"""

import asyncio
import logging
import os

from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.events.consumer_group import RedisConsumerGroup
from runtime.infrastructure.database import async_session
from runtime.infrastructure.llm.openai import OpenAIAdapter
from runtime.infrastructure.redis.client import get_redis_client
from runtime.shared.config import settings
from runtime.shared.logger import get_logger
from runtime.workers.signal_worker import SignalWorker

logger: logging.Logger = get_logger(__name__)


async def main() -> None:
    """Arranca el worker runtime y procesa mensajes indefinidamente."""
    from runtime.shared.config import validate_worker_settings
    validate_worker_settings()

    logger.info("Starting Worker Runtime...")

    redis = get_redis_client()

    # Consumer name: use HOSTNAME (unique per container/pod in Docker/Railway).
    # Falls back to "worker-1" for local development.
    # XAUTOCLAIM reclaims from any consumer in the group, so if this pod dies
    # and a new pod starts (even with a different HOSTNAME), it will recover
    # the pending messages of the dead pod on its first scan.
    consumer_name = os.environ.get("HOSTNAME", "worker-1")
    logger.info(
        "Worker consumer identity",
        extra={
            "consumer_name": consumer_name,
            "stream": settings.EVENT_BUS_STREAM,
            "group": settings.WORKER_GROUP_NAME,
        },
    )

    # 1. Configurar Consumer Group
    cg = RedisConsumerGroup(
        redis_client=redis,
        stream=settings.EVENT_BUS_STREAM,
        group=settings.WORKER_GROUP_NAME,
        consumer_name=consumer_name,
    )
    await cg.ensure_group()
    
    # 2. Configurar Cognitive Engine
    llm = OpenAIAdapter()
    engine = CognitiveEngine(llm)
    
    # 3. Construir Worker
    # Contexto genérico para la misión (S4 usa uno por defecto a menos que se obtenga de BD)
    mission_context = "Analizar el sentimiento, relevancia y métricas del post capturado."
    
    worker = SignalWorker(
        consumer_group=cg,
        session_factory=async_session,
        cognitive_engine=engine,
        llm_provider=llm,
        mission_context=mission_context,
    )
    
    stop_event = asyncio.Event()

    def handle_shutdown(signame):
        logger.info(f"Received {signame}, shutting down Worker Runtime...")
        stop_event.set()

    import signal
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda sig=sig: handle_shutdown(sig.name))
        except NotImplementedError:
            pass # Windows compatibility

    logger.info("Worker Runtime started and listening for events...")
    
    try:
        # Loop productivo, espera señal de cierre
        while not stop_event.is_set():
            # Procesar pending y luego nuevos
            await worker.process_next(count=10)
            
            # Pequeña pausa para no saturar Redis si no hay mensajes, interrumpible por stop_event
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down Worker Runtime (KeyboardInterrupt)...")
    except Exception as e:
        logger.error("Fatal error in Worker Runtime", exc_info=True, extra={"error": str(e)})
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
