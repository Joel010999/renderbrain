"""
runtime/workers/fingerprint.py

Estrategia de fingerprint V2 para la deduplicación de señales (A1.1).

A1.1 — Cambios respecto a V1:
    - Soporte de stories con namespace "story:" para evitar colisión con posts/reels.
    - Prioridad de extracción de native_id extendida para stories (storyId).
    - Namespace por content_type garantiza que:
        instagram:story:<id>  ≠  instagram:<id>  (post/reel)

Responsabilidad:
    Extraer una identidad estable y determinista de un RawSignalDetected
    para ser usada como clave de deduplicación (ProcessedSignal.fingerprint).

Principios de diseño:
- Función pura: no hace I/O, no tiene efectos secundarios.
- Determinista: el mismo contenido siempre genera el mismo fingerprint.
- Identidad estable: basada en IDs nativos o URLs canónicas.
- Fallo explícito: si no existe ninguna identidad estable, lanza FingerprintError.
- Sin colisiones: stories tienen namespace "story:", posts/reels no.

Estrategia por fuente (source):

    instagram (señales de perfil — A1.1):
        Detecta content_type desde raw_payload["content_type"]:
            "story":
                ID → raw_payload["data"]["id"] o ["storyId"]
                Formato: "instagram:story:<id>"
            "reel" | "post" | None (legacy):
                ID → raw_payload["data"]["id"] o ["shortCode"]
                Formato: "instagram:<id>"
        Fallback (todos los tipos): URL canónica limpia.
        Fallo: FingerprintError si ninguna identidad disponible.

    Fuentes genéricas:
        raw_payload["fingerprint_id"] → "<source>:<fingerprint_id>"
        Fallo: FingerprintError si ausente.

Prohibiciones explícitas:
    - NO usar hash del caption/content: mutable.
    - NO usar métricas: cambian con el tiempo.
    - NO usar timestamps: dependen del momento de captura.
    - NO usar el JSON completo: inestable ante campos nuevos de la API.
    - NO usar embeddings ni similitud semántica.
"""

from urllib.parse import urlparse, urlunparse

from runtime.contracts.raw_signal_detected import RawSignalDetected


class FingerprintError(Exception):
    """
    Error lanzado cuando no se puede extraer una identidad estable
    de la señal para generar el fingerprint de deduplicación.

    Se lanza en lugar de generar un fingerprint inestable o basura.
    El worker NO debe hacer XACK si recibe este error.
    """


def compute_fingerprint(raw_signal: RawSignalDetected) -> str:
    """
    Calcula el fingerprint de deduplicación para un RawSignalDetected.

    El fingerprint es una cadena estable y determinista que identifica
    unívocamente el contenido de la señal independientemente de cambios
    en sus métricas, timestamps o campos opcionales.

    Args:
        raw_signal: RawSignalDetected producido por cualquier BaseSensor.

    Returns:
        str: Fingerprint en formato "<source>:<identity>" o
             "instagram:story:<id>" para stories.
             Siempre no vacío y determinista para el mismo input.

    Raises:
        FingerprintError: Si no existe un campo de identidad estable
                          en el payload. El caller NO debe hacer XACK.
    """
    source = raw_signal.source
    payload = raw_signal.raw_payload

    if source == "instagram":
        return _fingerprint_instagram(payload)

    # Fuentes genéricas: buscar campo "fingerprint_id" explícito en el payload
    fp_id = payload.get("fingerprint_id")
    if isinstance(fp_id, str) and fp_id.strip():
        return f"{source}:{fp_id.strip()}"

    raise FingerprintError(
        f"Cannot compute fingerprint for source='{source}': "
        "raw_payload must contain a 'fingerprint_id' field with a stable identity. "
        "Do NOT use content, metrics, timestamps or the full JSON as fingerprint."
    )


def _fingerprint_instagram(payload: dict) -> str:
    """
    Estrategia de fingerprint V2 para señales de fuente 'instagram'.

    Detecta content_type desde payload["content_type"] para aplicar
    el namespace correcto y evitar colisiones entre stories y posts/reels.

    Prioridad de identidad:
        1. ID nativo desde payload["data"]["id"] o variantes por content_type
        2. URL canónica desde payload["url_queried"] (fallback legacy)

    Namespaces:
        Stories: "instagram:story:<id>"
        Posts/Reels/Legacy: "instagram:<id>"

    Raises:
        FingerprintError: Si ninguna identidad estable está disponible.
    """
    content_type = payload.get("content_type")  # "story" | "reel" | "post" | None
    data = payload.get("data")

    # --- Stories: namespace dedicado "story:" para evitar colisión ---
    if content_type == "story":
        if isinstance(data, dict):
            # stories pueden tener id, storyId, o shortCode
            story_id = (
                _safe_str(data.get("id"))
                or _safe_str(data.get("storyId"))
                or _safe_str(data.get("shortCode"))
            )
            if story_id:
                return f"instagram:story:{story_id}"

        # Fallback URL para stories
        url_queried = payload.get("url_queried")
        if isinstance(url_queried, str) and url_queried.strip():
            clean_url = _strip_query_params(url_queried.strip())
            if clean_url:
                return f"instagram:story:{clean_url}"

        raise FingerprintError(
            "Cannot compute Instagram story fingerprint: "
            "raw_payload['data']['id'] (or 'storyId') and raw_payload['url_queried'] "
            "are both missing or empty."
        )

    # --- Posts y Reels (incluyendo legacy sin content_type): namespace estándar ---
    if isinstance(data, dict):
        native_id = (
            _safe_str(data.get("id"))
            or _safe_str(data.get("shortCode"))
        )
        if native_id:
            return f"instagram:{native_id}"

    # Fallback legacy: URL canónica sin query params
    url_queried = payload.get("url_queried")
    if isinstance(url_queried, str) and url_queried.strip():
        clean_url = _strip_query_params(url_queried.strip())
        if clean_url:
            return f"instagram:{clean_url}"

    raise FingerprintError(
        "Cannot compute Instagram fingerprint: "
        "raw_payload['data']['id'] (or 'shortCode') and raw_payload['url_queried'] "
        "are both missing or empty. "
        "Ensure the Instagram adapter returns native post IDs."
    )


def _safe_str(value) -> str | None:
    """Convierte a str no vacío o retorna None."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _strip_query_params(url: str) -> str:
    """
    Elimina los query params de una URL, conservando scheme, host y path.

    Ejemplo:
        "https://www.instagram.com/p/ABC/?hl=es&igshid=xyz"
        → "https://www.instagram.com/p/ABC/"

    Args:
        url: URL completa, posiblemente con query params de tracking.

    Returns:
        URL limpia sin query params ni fragment.
    """
    parsed = urlparse(url)
    clean = parsed._replace(query="", fragment="")
    return urlunparse(clean)
