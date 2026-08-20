"""
runtime/engines/normalizer/engine.py

NormalizerEngine — Motor de normalización mínimo para el First Signal Flow.

Responsabilidad única:
    RawSignalDetected → CanonicalSignal (estructural, sin IA ni análisis semántico).

Principios de diseño:
- Implementa BaseNormalizer: puro, no conoce Redis, PostgreSQL ni EventEnvelope.
- La asignación de source_event_id es responsabilidad del orquestador:
  normalize() produce un CanonicalSignal con un UUID temporal en source_event_id
  (marcado en docstring). El orquestador reemplaza ese campo con el event_id real
  del EventEnvelope usando model_copy(update={...}).
- Normalización mínima estructural: extrae campos convencionales del raw_payload
  (content, author, language, metrics) sin transformaciones lingüísticas ni IA.
- Si un campo opcional no está en raw_payload, queda None según el contrato.

Reglas de extracción desde raw_payload:
    content  → "content" | "body" | "title" | str(raw_payload) como fallback
    author   → "author"  | "username" | "user" | None
    language → "language" | "lang" | None
    metrics  → "metrics" (dict) ya presente, o None — sin inferencia de campos.

Convenciones de trazabilidad temporal:
    captured_at  → heredado de RawSignalDetected (timezone-aware en UTC).
    normalized_at → generado automáticamente por el contrato CanonicalSignal.
    source_event_id → UUID placeholder interno; el orquestador lo reemplaza.
"""

from datetime import datetime

from runtime.contracts.canonical_signal import CanonicalSignalData
from runtime.contracts.interfaces import BaseNormalizer
from runtime.contracts.raw_signal_detected import RawSignalDetected


class NormalizerEngine(BaseNormalizer):
    """
    Motor de normalización mínimo: RawSignalDetected → CanonicalSignalData.

    Puro y sin efectos secundarios. No conoce Redis, PostgreSQL ni EventEnvelope.

    Uso típico (desde el orquestador):
        engine = NormalizerEngine()
        canonical_data = await engine.normalize(raw_signal)
        # Asignar trazabilidad real desde el EventEnvelope:
        canonical = CanonicalSignal(
            **canonical_data.model_dump(),
            source_event_id=envelope.event_id
        )
        await repo.save(canonical)
    """

    async def normalize(self, signal: RawSignalDetected) -> CanonicalSignalData:
        """
        Transforma un RawSignalDetected en un CanonicalSignalData.

        No asigna source_event_id. El orquestador es responsable de
        instanciar el CanonicalSignal definitivo con el event_id del EventEnvelope.

        Args:
            signal: RawSignalDetected producido por un BaseSensor.

        Returns:
            CanonicalSignalData con los datos puros normalizados.
        """
        if signal.source == "instagram":
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
            # id y normalized_at se autogeneran con defaults en el contrato.
        )

    def _normalize_instagram(self, signal: RawSignalDetected) -> CanonicalSignalData:
        """Normaliza un payload crudo proveniente de Instagram (Apify)."""
        payload = signal.raw_payload
        data = payload.get("data")

        if not isinstance(data, dict):
            raise ValueError("Invalid Instagram raw_payload: missing or invalid 'data' dictionary.")

        # content: extraído del caption o texto principal del post/reel.
        content = data.get("caption") or ""
        if not isinstance(content, str):
            content = str(content)

        # author: username u ownerUsername equivalente
        author = data.get("ownerUsername") or data.get("username")
        if not isinstance(author, str):
            author = str(author) if author is not None else None

        # captured_at: timestamp real de la publicación. Fallback a signal.captured_at
        captured_at_str = data.get("timestamp")
        captured_at = signal.captured_at
        if captured_at_str:
            try:
                # Python 3.12 soporta "Z" en fromisoformat
                captured_at = datetime.fromisoformat(captured_at_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        # metrics: extraer únicamente métricas numéricas simples
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
        # Fallback: serializar el payload completo como texto
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

        Solo acepta el campo "metrics" si es un dict con valores numéricos
        (float o int). No infiere métricas desde otros campos.
        Devuelve None si no existe o no es un dict de numéricas válidas.
        """
        raw_metrics = payload.get("metrics")
        if not isinstance(raw_metrics, dict):
            return None
        # Filtrar sólo los pares con valores numéricos válidos
        filtered = {
            k: v
            for k, v in raw_metrics.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return filtered if filtered else None
