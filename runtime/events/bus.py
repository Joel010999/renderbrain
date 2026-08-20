"""
runtime/events/bus.py

Event Bus de RenderBrain sobre Redis Streams.

Por qué Redis Streams sobre Pub/Sub:
    - Retención: los mensajes persisten en el stream y pueden releerse.
    - Trazabilidad: cada entrada tiene un Entry ID secuencial de Redis.
    - Base para consumer groups y replay en sprints futuros sin cambiar
      la API pública (publish / read).

Separación de identidades:
    event_id       → UUID de RenderBrain, viaja serializado en el campo
                     "data" del entry. Es la identidad del evento.
    Redis Entry ID → posición técnica del entry en el stream (ej. "1-0").
                     Lo usa read() como cursor; nunca reemplaza event_id.

Inyección de dependencias:
    RedisEventBus recibe un cliente redis.asyncio.Redis ya instanciado.
    El llamador gestiona el ciclo de vida del cliente (aclose).
    El stream es parametrizable para facilitar el aislamiento en tests.
"""

import json
import logging
from typing import Any

import redis.asyncio as aioredis

from runtime.contracts.event_envelope import EventEnvelope
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


class RedisEventBus:
    """
    Event Bus mínimo que publica y lee EventEnvelopes en un Redis Stream.

    Métodos públicos
    ----------------
    publish(event)          → str   (Redis Entry ID)
    read(count, last_id)    → list[EventEnvelope]
    """

    def __init__(self, redis_client: aioredis.Redis, stream: str) -> None:
        """
        Inicializa el bus con un cliente Redis ya abierto y un nombre de stream.

        Args:
            redis_client: Cliente redis.asyncio activo. El llamador es
                          responsable de invocar aclose() al finalizar.
            stream:       Nombre del Redis Stream donde se publican/leen
                          eventos (ej. "renderbrain:events:main").
        """
        self._redis = redis_client
        self._stream = stream

    # ------------------------------------------------------------------
    # Publicación
    # ------------------------------------------------------------------

    async def publish(self, event: EventEnvelope) -> str:
        """
        Serializa el EventEnvelope a JSON y lo escribe en el Redis Stream.

        El JSON completo del envelope se almacena en el campo "data" del
        entry para mantener atomicidad: un entry = un evento completo.

        Args:
            event: EventEnvelope a publicar.

        Returns:
            str: Entry ID asignado por Redis (ej. "1753315200000-0").
                 Representa la posición técnica en el stream, distinta
                 del event_id del envelope.

        Raises:
            redis.RedisError: Si la escritura en el stream falla.
        """
        data: str = event.model_dump_json()

        entry_id: str = await self._redis.xadd(
            self._stream,
            {"data": data},
        )

        logger.info(
            "Event published",
            extra={
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "correlation_id": str(event.correlation_id) if event.correlation_id else None,
                "stream": self._stream,
                "redis_entry_id": entry_id,
            },
        )

        return entry_id

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    async def read(
        self,
        count: int = 10,
        last_id: str = "0-0",
    ) -> list[EventEnvelope]:
        """
        Lee entries del Redis Stream y los deserializa como EventEnvelopes.

        Usa XRANGE para leer desde last_id (inclusive) hasta el final,
        limitando a `count` entradas. Apropiado para lectura simple sin
        consumer groups.

        Args:
            count:    Número máximo de eventos a retornar.
            last_id:  Entry ID de Redis desde donde leer (inclusive).
                      "0-0" lee desde el inicio del stream.

        Returns:
            list[EventEnvelope]: Eventos deserializados. El orden es
            cronológico (el más antiguo primero).

        Raises:
            redis.RedisError: Si la lectura del stream falla.
            pydantic.ValidationError: Si algún entry no puede deserializarse.
        """
        raw_entries: list[tuple[str, dict[str, Any]]] = await self._redis.xrange(
            self._stream,
            min=last_id,
            max="+",
            count=count,
        )

        envelopes: list[EventEnvelope] = []
        for _entry_id, fields in raw_entries:
            raw_json: str = fields["data"]
            envelope = EventEnvelope.model_validate_json(raw_json)
            envelopes.append(envelope)

        logger.debug(
            "Events read from stream",
            extra={
                "count": len(envelopes),
                "stream": self._stream,
                "from_id": last_id,
            },
        )

        return envelopes
