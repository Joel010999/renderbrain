"""
runtime/infrastructure/redis/probes.py

Función de comprobación de disponibilidad de Redis.

Crea un cliente temporal con get_redis_client() y ejecuta PING.
No expone URLs, credenciales ni mensajes de excepción internos —
solo retorna bool para que el endpoint /ready traduzca a HTTP.
"""

import logging

from runtime.infrastructure.redis.client import get_redis_client
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


async def check_redis() -> bool:
    """
    Verifica la disponibilidad de Redis ejecutando PING.

    Crea un cliente temporal y lo cierra en el bloque finally para
    garantizar que no haya leaks de conexión aunque falle el PING.

    Returns:
        True  — Redis respondió al PING correctamente.
        False — La conexión o el PING fallaron por cualquier motivo.
    """
    client = get_redis_client()
    try:
        await client.ping()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Redis health check failed",
            extra={"error_type": type(exc).__name__},
        )
        return False
    finally:
        await client.aclose()
