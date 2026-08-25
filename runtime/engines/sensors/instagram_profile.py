"""
runtime/engines/sensors/instagram_profile.py

InstagramProfileSensor — Sensor de captura autónoma de perfiles de Instagram.

A1.1 — Agente 1 Autónomo.

Responsabilidad única:
    Dado un username de Instagram, recolectar todo el contenido nuevo del perfil
    (Posts, Reels, Stories) y empaquetar cada ítem como un RawSignalDetected
    independiente con el mismo mission_id.

Principios de diseño:
    - detect() → list[RawSignalDetected]: un evento por ítem (Posts + Reels).
    - detect_stories() → list[RawSignalDetected]: stories separadas.
    - Si un ítem individual es inválido, se hace skip + warning (no se aborta el lote).
    - Si stories falla completamente (ApifyStoriesUnavailableError), se loguea
      como warning y se retorna lista vacía — NO propagación fatal.
    - El raw_payload conserva íntegramente el ítem crudo de Apify más:
        - profile_username: username de origen (provenance)
        - content_type:     "post" | "reel" | "story" (determinista desde Apify)
        - data:             ítem crudo de Apify sin transformación

Constantes del sensor:
    SENSOR_NAME = "instagram_profile_sensor"
    SOURCE_NAME = "instagram"
"""

from __future__ import annotations

import logging
from typing import Any, Protocol
from uuid import UUID

from runtime.contracts.interfaces import BaseSensor
from runtime.contracts.raw_signal_detected import RawSignalDetected

SENSOR_NAME: str = "instagram_profile_sensor"
SOURCE_NAME: str = "instagram"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols — interfaces estructurales para inyección de dependencias
# ---------------------------------------------------------------------------

class ProfileFetcher(Protocol):
    """Interfaz estructural mínima requerida por InstagramProfileSensor.

    Cualquier objeto que implemente estos métodos satisface el contrato
    sin herencia explícita (structural subtyping).
    ApifyInstagramAdapter y los FakeAdapter de tests lo cumplen.
    """

    def fetch_profile_posts(
        self,
        username: str,
        limit: int = 10,
        results_type: str = "posts",
    ) -> list[dict[str, Any]]:
        ...

    def fetch_profile_stories(
        self,
        username: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        ...


# ---------------------------------------------------------------------------
# Excepciones de dominio del sensor
# ---------------------------------------------------------------------------

class InstagramProfileSensorError(Exception):
    """Error base del dominio para InstagramProfileSensor."""


class InstagramProfileSensorAdapterError(InstagramProfileSensorError):
    """El adaptador lanzó una excepción que impidió la captura total."""


# ---------------------------------------------------------------------------
# Sensor principal
# ---------------------------------------------------------------------------

class InstagramProfileSensor:
    """Sensor de dominio que captura señales de perfiles de Instagram vía Apify.

    Recolecta Posts, Reels y opcionalmente Stories de un perfil público.
    Cada ítem se convierte en un RawSignalDetected independiente con el
    mismo mission_id, permitiendo la acumulación estratégica en el Agente 2.

    Args:
        mission_id:   UUID de la misión de perfil.
        username:     Username limpio de Instagram (sin @, sin URL).
        adapter:      Instancia de ApifyInstagramAdapter u otro ProfileFetcher.
        post_limit:   Máximo de posts a recolectar (default: 10).
        reel_limit:   Máximo de reels a recolectar (default: 10).
        story_limit:  Máximo de stories a recolectar (default: 20).
    """

    def __init__(
        self,
        mission_id: UUID,
        username: str,
        adapter: ProfileFetcher,
        post_limit: int = 10,
        reel_limit: int = 10,
        story_limit: int = 20,
    ) -> None:
        self._mission_id = mission_id
        self._username = username
        self._adapter = adapter
        self._post_limit = post_limit
        self._reel_limit = reel_limit
        self._story_limit = story_limit

    async def detect(self) -> list[RawSignalDetected]:
        """Recolecta Posts y Reels del perfil.

        Cada ítem de Apify genera un RawSignalDetected independiente.
        Los ítems inválidos (sin datos mínimos) se saltean con warning.

        Returns:
            Lista de RawSignalDetected (puede estar vacía si Apify no devuelve nada).

        Raises:
            InstagramProfileSensorAdapterError: si el adapter falla completamente.
        """
        signals: list[RawSignalDetected] = []

        # --- Posts ---
        posts = self._fetch_safe(
            lambda: self._adapter.fetch_profile_posts(
                self._username, limit=self._post_limit, results_type="posts"
            ),
            label="posts",
        )
        for item in posts:
            signal = self._build_signal(item, content_type="post")
            if signal:
                signals.append(signal)

        # --- Reels ---
        reels = self._fetch_safe(
            lambda: self._adapter.fetch_profile_posts(
                self._username, limit=self._reel_limit, results_type="reels"
            ),
            label="reels",
        )
        for item in reels:
            signal = self._build_signal(item, content_type="reel")
            if signal:
                signals.append(signal)

        logger.info(
            "InstagramProfileSensor: %d señales recolectadas (posts+reels) para '%s'",
            len(signals),
            self._username,
        )
        return signals

    async def detect_stories(self) -> list[RawSignalDetected]:
        """Recolecta Stories activas del perfil.

        Soft failure: si el adapter de stories falla, retorna lista vacía.
        Nunca propaga la excepción — el Scheduler debe llamar a este método
        de forma independiente al job principal.

        Returns:
            Lista de RawSignalDetected para stories (puede estar vacía).
        """
        # Importamos aquí para evitar dependencia circular en módulos que
        # solo necesitan el sensor (no el adaptador concreto).
        from runtime.infrastructure.apify.adapter import ApifyStoriesUnavailableError

        signals: list[RawSignalDetected] = []

        try:
            raw_items = self._adapter.fetch_profile_stories(
                self._username, limit=self._story_limit
            )
        except ApifyStoriesUnavailableError as exc:
            logger.warning(
                "Stories no disponibles para '%s' — continuando sin stories. Razón: %s",
                self._username,
                exc,
            )
            return []
        except Exception as exc:
            logger.warning(
                "Error inesperado al obtener stories para '%s': %s — continuando sin stories.",
                self._username,
                type(exc).__name__,
            )
            return []

        for item in raw_items:
            signal = self._build_signal(item, content_type="story")
            if signal:
                signals.append(signal)

        logger.info(
            "InstagramProfileSensor: %d stories recolectadas para '%s'",
            len(signals),
            self._username,
        )
        return signals

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    def _fetch_safe(
        self,
        fetch_fn,
        label: str,
    ) -> list[dict[str, Any]]:
        """Ejecuta una función de fetch capturando errores parciales.

        Si falla completamente, loguea el error y retorna lista vacía.
        No es una excepción fatal — el sensor sigue procesando los otros tipos.
        """
        try:
            return fetch_fn()
        except Exception as exc:
            logger.warning(
                "InstagramProfileSensor: fallo al obtener %s para '%s': %s — skipping.",
                label,
                self._username,
                type(exc).__name__,
            )
            return []

    def _build_signal(
        self,
        item: dict[str, Any],
        content_type: str,
    ) -> RawSignalDetected | None:
        """Construye un RawSignalDetected desde un ítem crudo de Apify.

        Si el ítem no tiene datos mínimos, retorna None con warning.

        Args:
            item:         Ítem crudo del dataset de Apify.
            content_type: "post" | "reel" | "story" — determinado externamente.

        Returns:
            RawSignalDetected o None si el ítem es inválido.
        """
        if not isinstance(item, dict):
            logger.warning(
                "InstagramProfileSensor: ítem inválido (no es dict) — skip. "
                "profile='%s' content_type='%s'",
                self._username,
                content_type,
            )
            return None

        # raw_payload conserva íntegramente el ítem crudo de Apify más
        # metadatos de provenance y clasificación para el normalizer.
        raw_payload: dict = {
            "profile_username": self._username,
            "content_type": content_type,
            "data": item,
        }

        try:
            return RawSignalDetected(
                sensor=SENSOR_NAME,
                source=SOURCE_NAME,
                mission_id=self._mission_id,
                raw_payload=raw_payload,
            )
        except Exception as exc:
            logger.warning(
                "InstagramProfileSensor: no se pudo construir RawSignalDetected "
                "para ítem de '%s' (%s): %s — skip.",
                self._username,
                content_type,
                type(exc).__name__,
            )
            return None
