"""
ApifyInstagramAdapter — S2.1
=============================
Único punto de contacto entre RenderBrain y el SDK de Apify.
Ningún otro módulo del proyecto debe importar ``apify-client`` directamente.

Responsabilidades:
- Validar token, URL y límite *antes* de tocar la red.
- Trasladar ``limit`` al parámetro ``resultsLimit`` del Actor para que Apify
  deje de procesar en cuanto alcance el máximo solicitado (control de costos).
- Normalizar errores externos en excepciones propias de infraestructura.

Lo que NO hace (restricciones S2.1):
- No crea InstagramSensor ni eventos de dominio.
- No implementa retries, circuit breakers ni DLQ.
- No publica en Redis ni escribe en base de datos.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

# apify-client se importa *aquí* y en ningún otro módulo del proyecto.
from apify_client import ApifyClient

from runtime.shared.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de control de costos
# ---------------------------------------------------------------------------
_LIMIT_MIN: int = 1
_LIMIT_MAX: int = 10  # tope duro; S2.x podrá ampliarlo de forma controlada


# ---------------------------------------------------------------------------
# Excepciones propias del adaptador (infraestructura, no dominio)
# ---------------------------------------------------------------------------

class ApifyAdapterError(Exception):
    """Error base del adaptador; nunca incluye el token en su mensaje."""


class ApifyTokenMissingError(ApifyAdapterError):
    """APIFY_API_TOKEN no está configurado en el entorno."""


class ApifyInvalidURLError(ApifyAdapterError):
    """La URL proporcionada está vacía o no es una URL HTTP/HTTPS válida."""


class ApifyInvalidLimitError(ApifyAdapterError):
    """El parámetro ``limit`` está fuera del rango permitido [1, 10]."""


class ApifyActorRunError(ApifyAdapterError):
    """El Actor de Apify no terminó en estado SUCCEEDED."""


class ApifyEmptyDatasetError(ApifyAdapterError):
    """El dataset del Actor está vacío o no devolvió ítems."""


class ApifyUnexpectedResponseError(ApifyAdapterError):
    """La respuesta de Apify tiene un formato inesperado."""


# ---------------------------------------------------------------------------
# Adaptador
# ---------------------------------------------------------------------------

class ApifyInstagramAdapter:
    """Adaptador de infraestructura para el Actor de Instagram en Apify.

    Usage::

        adapter = ApifyInstagramAdapter()
        result = adapter.fetch_post("https://www.instagram.com/p/XXXX/", limit=1)

    Args:
        actor_id: ID del Actor de Apify. Si se omite, se toma de
            ``settings.APIFY_INSTAGRAM_ACTOR_ID``.

    Raises:
        ApifyTokenMissingError: si ``APIFY_API_TOKEN`` no está en el entorno.
    """

    def __init__(self, actor_id: str | None = None) -> None:
        self._actor_id: str = actor_id or settings.APIFY_INSTAGRAM_ACTOR_ID
        # La validación del token se hace en fetch_post para no fallar en
        # tiempo de construcción durante tests unitarios que mockean el cliente.
        self._actor_id = actor_id or settings.APIFY_INSTAGRAM_ACTOR_ID

    # ------------------------------------------------------------------
    # Interfaz pública
    # ------------------------------------------------------------------

    def fetch_post(self, url: str, limit: int = 1) -> list[dict[str, Any]]:
        """Obtiene datos de una publicación pública de Instagram vía Apify.

        Args:
            url:   URL pública de la publicación (debe comenzar con
                   ``https://www.instagram.com/``).
            limit: Cantidad máxima de ítems a solicitar. Rango válido: [1, 10].
                   Se traslada directamente a ``resultsLimit`` del Actor para
                   que Apify no procese más ítems de los necesarios.

        Returns:
            Lista de dicts; normalmente contiene un único elemento con los
            metadatos de la publicación.

        Raises:
            ApifyTokenMissingError:       token ausente en el entorno.
            ApifyInvalidURLError:         URL vacía o con esquema inválido.
            ApifyInvalidLimitError:       limit fuera de [1, 10].
            ApifyActorRunError:           Actor no finalizó con SUCCEEDED.
            ApifyEmptyDatasetError:       dataset vacío.
            ApifyUnexpectedResponseError: respuesta en formato inesperado.
        """
        # 1. Validaciones previas (no tocan la red)
        token = self._resolve_token()
        self._validate_url(url)
        self._validate_limit(limit)

        # 2. Ejecutar el Actor
        logger.info("Apify: iniciando Actor '%s' con limit=%d", self._actor_id, limit)
        items = self._run_actor(token=token, url=url, limit=limit)
        logger.info("Apify: Actor finalizó, %d ítem(s) recibido(s)", len(items))
        return items

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_token() -> str:
        """Devuelve el token en texto plano; lo mantiene fuera de logs/repr."""
        secret = settings.APIFY_API_TOKEN
        if secret is None:
            raise ApifyTokenMissingError(
                "APIFY_API_TOKEN no está configurado. "
                "Añadilo al archivo .env (ver .env.example)."
            )
        # get_secret_value() es el único lugar donde el token se expone como str.
        return secret.get_secret_value()

    @staticmethod
    def _validate_url(url: str) -> None:
        if not url or not url.strip():
            raise ApifyInvalidURLError("La URL no puede estar vacía.")
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ApifyInvalidURLError(
                f"URL inválida: se esperaba esquema http/https con host, "
                f"se recibió: '{url}'"
            )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if limit < _LIMIT_MIN:
            raise ApifyInvalidLimitError(
                f"limit={limit} es inválido: debe ser >= {_LIMIT_MIN}."
            )
        if limit > _LIMIT_MAX:
            raise ApifyInvalidLimitError(
                f"limit={limit} supera el máximo permitido de {_LIMIT_MAX}. "
                "Incrementá _LIMIT_MAX con control explícito de costos."
            )

    def _run_actor(
        self,
        *,
        token: str,
        url: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Llama al Actor de Apify y devuelve la lista de ítems del dataset."""
        client = ApifyClient(token)  # token nunca se loguea

        run_input = {
            "directUrls": [url],
            # resultsLimit es el parámetro del actor apify/instagram-scraper
            # que controla cuántos ítems procesa Apify antes de detenerse.
            "resultsLimit": limit,
            "resultsType": "posts",
        }

        try:
            run = client.actor(self._actor_id).call(run_input=run_input)
        except Exception as exc:
            # No re-lanzamos exc directamente para evitar que el traceback
            # exponga el token si está embebido en la excepción original.
            raise ApifyActorRunError(
                f"El Actor '{self._actor_id}' falló durante la ejecución. "
                f"Motivo: {type(exc).__name__}"
            ) from exc

        # Verificar estado final del run.
        # En apify-client>=3.x call() devuelve un objeto Run (Pydantic BaseModel),
        # no un dict. Los atributos son snake_case: run.status, run.default_dataset_id.
        if run is None or run.status != "SUCCEEDED":
            status = run.status if run is not None else "None"
            raise ApifyActorRunError(
                f"El Actor finalizó con estado inesperado: '{status}'. "
                "Se esperaba 'SUCCEEDED'."
            )

        # Leer dataset — atributo snake_case del modelo Run
        dataset_id = run.default_dataset_id
        if not dataset_id:
            raise ApifyUnexpectedResponseError(
                "La respuesta del Actor no contiene 'default_dataset_id'."
            )

        try:
            raw_items = list(
                client.dataset(dataset_id).iterate_items()
            )
        except Exception as exc:
            raise ApifyUnexpectedResponseError(
                f"No se pudo leer el dataset '{dataset_id}': {type(exc).__name__}"
            ) from exc

        if not raw_items:
            raise ApifyEmptyDatasetError(
                f"El dataset '{dataset_id}' está vacío. "
                "Verificá que la URL sea pública y el Actor esté activo."
            )

        # Verificación superficial de estructura (lista de dicts)
        if not isinstance(raw_items, list) or not all(
            isinstance(item, dict) for item in raw_items
        ):
            raise ApifyUnexpectedResponseError(
                "El dataset devuelto no tiene el formato esperado (lista de dicts)."
            )

        return raw_items
