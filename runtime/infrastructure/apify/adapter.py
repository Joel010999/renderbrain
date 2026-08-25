"""
ApifyInstagramAdapter — A1.1
=============================
Único punto de contacto entre RenderBrain y el SDK de Apify.
Ningún otro módulo del proyecto debe importar ``apify-client`` directamente.

Responsabilidades:
- Validar token, URL/username y límite *antes* de tocar la red.
- fetch_post: obtiene datos de una publicación pública (target_type=post).
- fetch_profile_posts: obtiene posts o reels de un perfil (target_type=profile).
- fetch_profile_stories: obtiene stories activas de un perfil usando el actor
  dedicado apify/instagram-stories-scraper.

A1.1 — Cambios respecto a S2.1:
- _LIMIT_MAX ampliado de 10 a 50 para soportar límites de perfil.
- Nuevos métodos: fetch_profile_posts(), fetch_profile_stories().
- Nueva excepción: ApifyStoriesUnavailableError (fallo soft de stories, no fatal).
- _run_actor_for_profile() para profile scraping via directUrls con username.

Lo que NO hace:
- No crea InstagramSensor ni eventos de dominio.
- No implementa retries, circuit breakers ni DLQ.
- No publica en Redis ni escribe en base de datos.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from urllib.parse import urlparse

# apify-client se importa *aquí* y en ningún otro módulo del proyecto.
from apify_client import ApifyClient

from runtime.shared.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes de control de costos
# ---------------------------------------------------------------------------
_LIMIT_MIN: int = 1
_LIMIT_MAX: int = 50  # ampliado para soporte de perfiles (A1.1)


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
    """El parámetro ``limit`` está fuera del rango permitido [1, 50]."""


class ApifyActorRunError(ApifyAdapterError):
    """El Actor de Apify no terminó en estado SUCCEEDED."""


class ApifyEmptyDatasetError(ApifyAdapterError):
    """El dataset del Actor está vacío o no devolvió ítems."""


class ApifyUnexpectedResponseError(ApifyAdapterError):
    """La respuesta de Apify tiene un formato inesperado."""


class ApifyStoriesUnavailableError(ApifyAdapterError):
    """
    El actor de Stories falló o no está disponible.

    Este error es SOFT: el caller (InstagramProfileSensor) debe capturarlo,
    loguearlo como warning y continuar con Posts/Reels. No debe propagarse
    como error fatal al Scheduler.
    """


# ---------------------------------------------------------------------------
# Adaptador
# ---------------------------------------------------------------------------

class ApifyInstagramAdapter:
    """Adaptador de infraestructura para el Actor de Instagram en Apify.

    Soporta dos modos de operación:
    1. fetch_post(): URL directa de post/reel (target_type=post, legado S2.1).
    2. fetch_profile_posts(): username de perfil + resultsType (target_type=profile, A1.1).
    3. fetch_profile_stories(): username de perfil via actor dedicado (A1.1).

    Args:
        actor_id:        ID del Actor principal. Default: settings.APIFY_INSTAGRAM_ACTOR_ID.
        stories_actor_id: ID del Actor de stories. Default: settings.APIFY_INSTAGRAM_STORIES_ACTOR_ID.

    Raises:
        ApifyTokenMissingError: si ``APIFY_API_TOKEN`` no está en el entorno.
    """

    def __init__(
        self,
        actor_id: str | None = None,
        stories_actor_id: str | None = None,
    ) -> None:
        self._actor_id: str = actor_id or settings.APIFY_INSTAGRAM_ACTOR_ID
        self._stories_actor_id: str = (
            stories_actor_id or settings.APIFY_INSTAGRAM_STORIES_ACTOR_ID
        )

    # ------------------------------------------------------------------
    # Interfaz pública — post individual (legado, target_type=post)
    # ------------------------------------------------------------------

    def fetch_post(self, url: str, limit: int = 1) -> list[dict[str, Any]]:
        """Obtiene datos de una publicación pública de Instagram vía Apify.

        Args:
            url:   URL pública de la publicación (debe comenzar con
                   ``https://www.instagram.com/``).
            limit: Cantidad máxima de ítems a solicitar. Rango válido: [1, 50].

        Returns:
            Lista de dicts; normalmente contiene un único elemento con los
            metadatos de la publicación.

        Raises:
            ApifyTokenMissingError:       token ausente en el entorno.
            ApifyInvalidURLError:         URL vacía o con esquema inválido.
            ApifyInvalidLimitError:       limit fuera de [1, 50].
            ApifyActorRunError:           Actor no finalizó con SUCCEEDED.
            ApifyEmptyDatasetError:       dataset vacío.
            ApifyUnexpectedResponseError: respuesta en formato inesperado.
        """
        token = self._resolve_token()
        self._validate_url(url)
        self._validate_limit(limit)

        logger.info("Apify: iniciando Actor '%s' con limit=%d", self._actor_id, limit)
        items = self._run_actor(
            token=token,
            actor_id=self._actor_id,
            run_input={
                "directUrls": [url],
                "resultsLimit": limit,
                "resultsType": "posts",
            },
        )
        logger.info("Apify: Actor finalizó, %d ítem(s) recibido(s)", len(items))
        return items

    # ------------------------------------------------------------------
    # Interfaz pública — perfil (A1.1, target_type=profile)
    # ------------------------------------------------------------------

    def fetch_profile_posts(
        self,
        username: str,
        limit: int = 10,
        results_type: Literal["posts", "reels"] = "posts",
    ) -> list[dict[str, Any]]:
        """Obtiene posts o reels recientes de un perfil público de Instagram.

        Args:
            username:     Username limpio de Instagram (sin @, sin URL).
            limit:        Máximo de ítems. Rango: [1, 50].
            results_type: "posts" o "reels".

        Returns:
            Lista de dicts con los ítems del dataset de Apify.

        Raises:
            ApifyTokenMissingError, ApifyInvalidLimitError, ApifyActorRunError,
            ApifyEmptyDatasetError, ApifyUnexpectedResponseError.
        """
        token = self._resolve_token()
        self._validate_limit(limit)

        profile_url = f"https://www.instagram.com/{username}/"
        logger.info(
            "Apify: iniciando Actor '%s' — profile=%s resultsType=%s limit=%d",
            self._actor_id,
            username,
            results_type,
            limit,
        )
        items = self._run_actor(
            token=token,
            actor_id=self._actor_id,
            run_input={
                "directUrls": [profile_url],
                "resultsLimit": limit,
                "resultsType": results_type,
            },
        )
        logger.info(
            "Apify: Actor finalizó — profile=%s resultsType=%s items=%d",
            username,
            results_type,
            len(items),
        )
        return items

    def fetch_profile_stories(
        self,
        username: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Obtiene stories activas de un perfil de Instagram via actor dedicado.

        Usa ``APIFY_INSTAGRAM_STORIES_ACTOR_ID`` (apify/instagram-stories-scraper),
        que requiere autenticación vía sessionid cookie configurado en Apify.

        Args:
            username: Username limpio de Instagram.
            limit:    Máximo de stories. Rango: [1, 50].

        Returns:
            Lista de dicts con las stories disponibles.

        Raises:
            ApifyStoriesUnavailableError: fallo SOFT — el caller debe capturar,
                loguear y continuar con Posts/Reels. No es error fatal.
        """
        try:
            token = self._resolve_token()
            self._validate_limit(limit)
        except ApifyAdapterError as exc:
            raise ApifyStoriesUnavailableError(
                f"Stories no disponibles para '{username}': {exc}"
            ) from exc

        try:
            logger.info(
                "Apify: iniciando Actor de stories '%s' — profile=%s limit=%d",
                self._stories_actor_id,
                username,
                limit,
            )
            items = self._run_actor(
                token=token,
                actor_id=self._stories_actor_id,
                run_input={
                    "usernames": [username],
                    "resultsLimit": limit,
                },
            )
            logger.info(
                "Apify: Actor de stories finalizó — profile=%s stories=%d",
                username,
                len(items),
            )
            return items
        except (ApifyActorRunError, ApifyEmptyDatasetError, ApifyUnexpectedResponseError) as exc:
            raise ApifyStoriesUnavailableError(
                f"Stories no disponibles para '{username}': {type(exc).__name__} — {exc}"
            ) from exc

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
        actor_id: str,
        run_input: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Llama a un Actor de Apify y devuelve la lista de ítems del dataset."""
        client = ApifyClient(token)  # token nunca se loguea

        try:
            run = client.actor(actor_id).call(run_input=run_input)
        except Exception as exc:
            raise ApifyActorRunError(
                f"El Actor '{actor_id}' falló durante la ejecución. "
                f"Motivo: {type(exc).__name__}"
            ) from exc

        if run is None or run.status != "SUCCEEDED":
            status = run.status if run is not None else "None"
            raise ApifyActorRunError(
                f"El Actor finalizó con estado inesperado: '{status}'. "
                "Se esperaba 'SUCCEEDED'."
            )

        dataset_id = run.default_dataset_id
        if not dataset_id:
            raise ApifyUnexpectedResponseError(
                "La respuesta del Actor no contiene 'default_dataset_id'."
            )

        try:
            raw_items = list(client.dataset(dataset_id).iterate_items())
        except Exception as exc:
            raise ApifyUnexpectedResponseError(
                f"No se pudo leer el dataset '{dataset_id}': {type(exc).__name__}"
            ) from exc

        if not raw_items:
            raise ApifyEmptyDatasetError(
                f"El dataset '{dataset_id}' está vacío. "
                "Verificá que el perfil sea público y el Actor esté activo."
            )

        if not isinstance(raw_items, list) or not all(
            isinstance(item, dict) for item in raw_items
        ):
            raise ApifyUnexpectedResponseError(
                "El dataset devuelto no tiene el formato esperado (lista de dicts)."
            )

        return raw_items
