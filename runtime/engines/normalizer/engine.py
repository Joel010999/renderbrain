"""
runtime/engines/normalizer/engine.py

NormalizerEngine — Motor de normalización mínimo para el First Signal Flow.

A1.1 — Extensión para señales de perfiles de Instagram:
    - _normalize_instagram_profile(): detecta si el payload viene de
      InstagramProfileSensor (tiene raw_payload["content_type"]) y extrae
      content_type, native_id, source_account_username/name/id de forma
      determinista desde el payload real de Apify.
    - _normalize_instagram(): se mantiene para señales de target_type=post
      (instancia legacy, sin content_type explícito).

Principios de diseño:
- Implementa BaseNormalizer: puro, no conoce Redis, PostgreSQL ni EventEnvelope.
- La asignación de source_event_id es responsabilidad del orquestador.
- Normalización mínima estructural: extrae campos convencionales del raw_payload
  sin transformaciones lingüísticas ni IA.

Mapeo determinista de content_type desde payload de Apify:
    raw_payload["content_type"] == "reel"  → content_type = "reel"
    raw_payload["content_type"] == "post"  → content_type = "post"
    raw_payload["content_type"] == "story" → content_type = "story"
    Ausente o desconocido                  → content_type = None

Extracción de native_id (por prioridad):
    data["id"] → data["shortCode"] → data["storyId"] → None
"""

from datetime import datetime

from runtime.contracts.canonical_signal import CanonicalSignalData
from runtime.contracts.interfaces import BaseNormalizer
from runtime.contracts.raw_signal_detected import RawSignalDetected

# Valores válidos de content_type — mapeo determinista desde Apify
_VALID_CONTENT_TYPES = frozenset({"post", "reel", "story"})


class NormalizerEngine(BaseNormalizer):
    """
    Motor de normalización mínimo: RawSignalDetected → CanonicalSignalData.

    Puro y sin efectos secundarios. No conoce Redis, PostgreSQL ni EventEnvelope.
    """

    async def normalize(self, signal: RawSignalDetected) -> CanonicalSignalData:
        """
        Transforma un RawSignalDetected en un CanonicalSignalData.

        Detecta automáticamente si viene de InstagramProfileSensor
        (tiene raw_payload["content_type"]) y usa la ruta de normalización
        de perfil. Las señales de post individual legacy usan la ruta clásica.
        """
        if signal.source == "instagram":
            payload = signal.raw_payload
            # Si el payload tiene "content_type" explícito → es señal de perfil (A1.1)
            if "content_type" in payload:
                return self._normalize_instagram_profile(signal)
            # Sin "content_type" → señal legacy de post individual
            return self._normalize_instagram(signal)

        payload = signal.raw_payload

        content = self._extract_content(payload)
        author = self._extract_author(payload)
        language = self._extract_language(payload)
        metrics = self._extract_metrics(payload)

        return CanonicalSignalData(
            mission_id=signal.mission_id,
            source=signal.source,
            sensor=signal.sensor,
            content=content,
            author=author,
            language=language,
            metrics=metrics,
            captured_at=signal.captured_at,
        )

    def _normalize_instagram_profile(self, signal: RawSignalDetected) -> CanonicalSignalData:
        """
        Normaliza un payload proveniente de InstagramProfileSensor (A1.1).

        El payload tiene estructura:
            {
                "profile_username": "dimitris.tech",
                "content_type": "reel" | "post" | "story",
                "data": { ...ítem crudo de Apify... }
            }

        Mapea content_type determinísticamente desde raw_payload["content_type"].
        Extrae provenance completa de la cuenta de origen.
        """
        payload = signal.raw_payload
        data = payload.get("data")

        if not isinstance(data, dict):
            raise ValueError(
                "Invalid Instagram profile raw_payload: "
                "missing or invalid 'data' dictionary."
            )

        # --- content_type: determinista desde payload ---
        raw_ct = payload.get("content_type", "")
        content_type = raw_ct if raw_ct in _VALID_CONTENT_TYPES else None

        # --- native_id: prioridad id → shortCode → storyId → None ---
        native_id = (
            self._safe_str(data.get("id"))
            or self._safe_str(data.get("shortCode"))
            or self._safe_str(data.get("storyId"))
        )

        # --- provenance de cuenta ---
        source_account_username = (
            self._safe_str(data.get("ownerUsername"))
            or self._safe_str(payload.get("profile_username"))
        )
        source_account_name = self._safe_str(
            data.get("ownerFullName") or data.get("fullName")
        )
        source_account_id = self._safe_str(
            data.get("ownerId") or data.get("userId")
        )

        # --- content: caption / text / alt / empty ---
        content = data.get("caption") or data.get("text") or data.get("alt")
        content = str(content).strip() if content else ""

        # --- author: username del propietario ---
        author = source_account_username

        # --- captured_at: timestamp real de publicación ---
        captured_at_str = data.get("timestamp") or data.get("takenAtTimestamp")
        captured_at = signal.captured_at
        if captured_at_str:
            try:
                captured_at = datetime.fromisoformat(
                    str(captured_at_str).replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # --- metrics: engagement numérico ---
        metrics = {}
        for key in ["likesCount", "commentsCount", "viewsCount", "playsCount"]:
            val = data.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                metric_name = key.replace("Count", "")
                metrics[metric_name] = val

        return CanonicalSignalData(
            mission_id=signal.mission_id,
            source=signal.source,
            sensor=signal.sensor,
            content_type=content_type,
            native_id=native_id,
            source_account_username=source_account_username,
            source_account_name=source_account_name,
            source_account_id=source_account_id,
            content=content,
            author=author,
            language=None,
            metrics=metrics if metrics else None,
            captured_at=captured_at,
        )

    def _normalize_instagram(self, signal: RawSignalDetected) -> CanonicalSignalData:
        """Normaliza un payload crudo proveniente de Instagram (legacy S2.1)."""
        payload = signal.raw_payload
        data = payload.get("data")

        if not isinstance(data, dict):
            raise ValueError("Invalid Instagram raw_payload: missing or invalid 'data' dictionary.")

        # content: caption o texto principal
        content = data.get("caption")
        if not content or not str(content).strip():
            content = data.get("text")
        if not content or not str(content).strip():
            content = data.get("alt")
        content = str(content).strip() if content else ""

        # author: username
        author = data.get("ownerUsername") or data.get("username")
        if not isinstance(author, str):
            author = str(author) if author is not None else None

        # captured_at: timestamp real
        captured_at_str = data.get("timestamp")
        captured_at = signal.captured_at
        if captured_at_str:
            try:
                captured_at = datetime.fromisoformat(captured_at_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        # metrics
        metrics = {}
        for key in ["likesCount", "commentsCount", "viewsCount", "playsCount"]:
            val = data.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                metric_name = key.replace("Count", "")
                metrics[metric_name] = val

        # native_id para señales legacy (ayuda al fingerprint)
        native_id = (
            self._safe_str(data.get("id"))
            or self._safe_str(data.get("shortCode"))
        )

        return CanonicalSignalData(
            mission_id=signal.mission_id,
            source=signal.source,
            sensor=signal.sensor,
            content_type=None,  # legacy: no se infiere desde texto
            native_id=native_id,
            source_account_username=None,
            source_account_name=None,
            source_account_id=None,
            content=content,
            author=author,
            language=None,
            metrics=metrics if metrics else None,
            captured_at=captured_at,
        )

    # ------------------------------------------------------------------
    # Extracción privada desde raw_payload — sin transformaciones IA
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_str(value) -> str | None:
        """Convierte a str no vacío o retorna None."""
        if value is None:
            return None
        s = str(value).strip()
        return s if s else None

    @staticmethod
    def _extract_content(payload: dict) -> str:
        """
        Extrae el texto principal del payload.

        Precedencia: "content" → "body" → "title" → repr(payload) como último recurso.
        Siempre devuelve str no vacío.
        """
        for key in ("content", "body", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(payload)

    @staticmethod
    def _extract_author(payload: dict) -> str | None:
        """
        Extrae el autor del payload.

        Precedencia: "author" → "username" → "user" → None.
        """
        for key in ("author", "username", "user"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_language(payload: dict) -> str | None:
        """
        Extrae el código de idioma del payload.

        Precedencia: "language" → "lang" → None.
        """
        for key in ("language", "lang"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _extract_metrics(payload: dict) -> dict[str, float | int] | None:
        """
        Extrae métricas numéricas del payload.

        Solo acepta el campo "metrics" si es un dict con valores numéricos.
        Devuelve None si no existe o no es un dict de numéricas válidas.
        """
        raw_metrics = payload.get("metrics")
        if not isinstance(raw_metrics, dict):
            return None
        filtered = {
            k: v
            for k, v in raw_metrics.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return filtered if filtered else None
