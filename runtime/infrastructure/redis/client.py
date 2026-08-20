"""
runtime/infrastructure/redis/client.py

Cliente Redis asíncrono reutilizable para RenderBrain.

Principios de diseño:
- No se instancia a nivel de módulo para evitar conexiones prematuras al
  importar (el engine de SQLAlchemy sí lo hace; Redis no debe seguir ese
  patrón aquí porque no tenemos pool global en este sprint).
- La función de fábrica `get_redis_client` devuelve un cliente fresco con
  su propio pool interno; el llamador es responsable de invocar `aclose()`
  al finalizar.
- `decode_responses=True` garantiza que las respuestas lleguen como str,
  no como bytes, lo que simplifica el código de negocio.

Uso típico:
    client = get_redis_client()
    try:
        pong = await client.ping()
    finally:
        await client.aclose()

Uso como dependencia FastAPI (futuro):
    async def get_redis() -> AsyncGenerator[Redis, None]:
        client = get_redis_client()
        try:
            yield client
        finally:
            await client.aclose()
"""

import redis.asyncio as aioredis

from runtime.shared.config import settings


def get_redis_client() -> aioredis.Redis:
    """
    Crea y devuelve un cliente Redis asíncrono configurado desde REDIS_URL.

    El cliente gestiona su propio pool de conexiones interno (por defecto
    un pool de 10 conexiones). Invocar ``await client.aclose()`` cuando
    el cliente ya no sea necesario para liberar las conexiones.

    Returns:
        redis.asyncio.Redis: cliente listo para operaciones async.
    """
    return aioredis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
    )
