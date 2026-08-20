"""
runtime/events/consumer_group.py

Soporte de Redis Consumer Groups para el Event Bus de RenderBrain.

Responsabilidades:
    - Crear el consumer group de forma idempotente (tolera BUSYGROUP).
    - Leer mensajes nuevos vía XREADGROUP (cursor ">").
    - Leer/reclamar mensajes pending (PEL) cuando corresponda.
    - Hacer XACK explícito usando el Redis Entry ID.
    - Convertir entradas Redis a (redis_entry_id, EventEnvelope).

Separación de identidades (igual que el bus principal):
    redis_entry_id  → posición técnica del entry (ej. "1753315200000-0").
                      Es el ID que se usa en XACK.
    event_id        → UUID de RenderBrain serializado en el campo "data".
                      Nunca se usa como cursor ni para XACK.

Inyección de dependencias:
    RedisConsumerGroup recibe un cliente redis.asyncio.Redis ya instanciado.
    El llamador gestiona el ciclo de vida del cliente (aclose).
    stream, group y consumer_name son parametrizables para aislamiento en tests.

Restricciones S4.2:
    NO implementa DLQ, retry policy, backoff complejo, métricas ni autoscaling.
"""

import logging
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from runtime.contracts.event_envelope import EventEnvelope
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)

# Código de error que Redis devuelve cuando el grupo ya existe
_BUSYGROUP = "BUSYGROUP"


class RedisConsumerGroup:
    """
    Abstracción mínima sobre Redis Consumer Groups para RenderBrain.

    Métodos públicos
    ----------------
    ensure_group()                  → None   (idempotente)
    read_new(count, block_ms)       → list[tuple[str, EventEnvelope]]
    read_pending(count, min_idle_ms)→ list[tuple[str, EventEnvelope]]
    ack(entry_id)                   → None
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        stream: str,
        group: str,
        consumer_name: str,
    ) -> None:
        """
        Inicializa el consumer group con un cliente Redis ya abierto.

        Args:
            redis_client:  Cliente redis.asyncio activo. El llamador gestiona aclose().
            stream:        Nombre del Redis Stream (ej. "renderbrain:events:main").
            group:         Nombre del consumer group (ej. "signal-workers").
            consumer_name: Nombre de este consumidor dentro del grupo
                           (ej. "worker-1"). Debe ser único por instancia.
        """
        self._redis = redis_client
        self._stream = stream
        self._group = group
        self._consumer = consumer_name

    # ------------------------------------------------------------------
    # Gestión del grupo
    # ------------------------------------------------------------------

    async def ensure_group(self) -> None:
        """
        Crea el consumer group de forma idempotente.

        Usa XGROUP CREATE con MKSTREAM para crear el stream si no existe.
        Si el grupo ya existe (BUSYGROUP), ignora el error silenciosamente.
        El offset inicial es "$" — el grupo solo verá mensajes nuevos desde
        el momento de creación; mensajes previos permanecen sin cambios.

        Para leer desde el inicio del stream usar offset "0" en lugar de "$".

        Raises:
            redis.ResponseError: Si el error no es BUSYGROUP (error real).
        """
        try:
            await self._redis.xgroup_create(
                name=self._stream,
                groupname=self._group,
                id="0",          # leer desde el inicio del stream
                mkstream=True,   # crear el stream si no existe
            )
            logger.info(
                "Consumer group created",
                extra={
                    "stream": self._stream,
                    "group": self._group,
                },
            )
        except ResponseError as exc:
            if _BUSYGROUP in str(exc):
                logger.debug(
                    "Consumer group already exists — skipping creation",
                    extra={"stream": self._stream, "group": self._group},
                )
            else:
                raise

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    async def read_new(
        self,
        count: int = 10,
        block_ms: int | None = None,
    ) -> list[tuple[str, EventEnvelope]]:
        """
        Lee mensajes nuevos (no entregados a ningún consumidor aún).

        Usa XREADGROUP con cursor ">" para obtener únicamente mensajes
        que no han sido entregados previamente a ningún consumidor del grupo.
        Los mensajes leídos pasan a la PEL (Pending Entry List) del consumidor
        hasta que se haga XACK explícito.

        Args:
            count:    Máximo de mensajes a leer.
            block_ms: Milisegundos a bloquear esperando nuevos mensajes.
                      None = no bloquear (retorna inmediatamente).

        Returns:
            Lista de (redis_entry_id, EventEnvelope) en orden de inserción.
            Lista vacía si no hay mensajes nuevos.

        Raises:
            redis.RedisError: Si la lectura falla.
            pydantic.ValidationError: Si algún entry no puede deserializarse.
        """
        raw: list[Any] | None = await self._redis.xreadgroup(
            groupname=self._group,
            consumername=self._consumer,
            streams={self._stream: ">"},
            count=count,
            block=block_ms,
        )

        return self._parse_xreadgroup_response(raw)

    async def read_pending(
        self,
        count: int = 10,
        min_idle_ms: int = 0,
    ) -> list[tuple[str, EventEnvelope]]:
        """
        Reclama y lee mensajes pending (en la PEL) para este consumidor.

        Usa XAUTOCLAIM para reclamar mensajes que llevan al menos `min_idle_ms`
        milisegundos sin ser ACK'd. Útil para reprocesar mensajes que fallaron
        en una iteración anterior.

        Args:
            count:       Máximo de mensajes a reclamar.
            min_idle_ms: Milisegundos mínimos en PEL para reclamar el mensaje.
                         0 = reclamar todos los pending de este consumidor.

        Returns:
            Lista de (redis_entry_id, EventEnvelope). Lista vacía si no hay pending.

        Raises:
            redis.RedisError: Si la operación falla.
        """
        result = await self._redis.xautoclaim(
            name=self._stream,
            groupname=self._group,
            consumername=self._consumer,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        # xautoclaim retorna (next_start_id, entries, deleted_ids)
        # entries tiene el mismo formato que xreadgroup: lista de (id, fields)
        entries = result[1] if result and len(result) > 1 else []
        return self._parse_entries(entries)

    # ------------------------------------------------------------------
    # Confirmación
    # ------------------------------------------------------------------

    async def ack(self, entry_id: str) -> None:
        """
        Confirma el procesamiento de un mensaje con XACK.

        Elimina el mensaje de la PEL (Pending Entry List) del consumidor.
        Debe llamarse ÚNICAMENTE después de que el procesamiento completo
        haya terminado con éxito (commit de DB incluido).

        Args:
            entry_id: Redis Entry ID del mensaje (ej. "1753315200000-0").
                      Este es el ID técnico de Redis, DISTINTO al event_id
                      del EventEnvelope.

        Raises:
            redis.RedisError: Si el XACK falla.
        """
        await self._redis.xack(self._stream, self._group, entry_id)
        logger.info(
            "Message acknowledged",
            extra={
                "stream": self._stream,
                "group": self._group,
                "entry_id": entry_id,
            },
        )

    # ------------------------------------------------------------------
    # Parseo interno — convierte formato Redis → (entry_id, EventEnvelope)
    # ------------------------------------------------------------------

    def _parse_xreadgroup_response(
        self,
        raw: list[Any] | None,
    ) -> list[tuple[str, EventEnvelope]]:
        """
        Convierte la respuesta de XREADGROUP a lista de (entry_id, EventEnvelope).

        XREADGROUP retorna: list[tuple[stream_name, list[tuple[entry_id, fields]]]]
        """
        if not raw:
            return []

        result: list[tuple[str, EventEnvelope]] = []
        for _stream_name, entries in raw:
            result.extend(self._parse_entries(entries))
        return result

    def _parse_entries(
        self,
        entries: list[tuple[str, dict[str, Any]]],
    ) -> list[tuple[str, EventEnvelope]]:
        """
        Convierte una lista de entries Redis a (entry_id, EventEnvelope).

        Args:
            entries: Lista de (redis_entry_id, fields_dict) tal como retorna
                     xreadgroup o xautoclaim.
        """
        result: list[tuple[str, EventEnvelope]] = []
        for entry_id, fields in entries:
            raw_json: str = fields["data"]
            envelope = EventEnvelope.model_validate_json(raw_json)
            result.append((entry_id, envelope))
            logger.debug(
                "Entry parsed",
                extra={
                    "redis_entry_id": entry_id,
                    "event_id": str(envelope.event_id),
                    "event_type": envelope.event_type,
                },
            )
        return result
