"""
runtime/workers/fingerprint.py

Estrategia de fingerprint V1 para la deduplicación de señales (S4.3).

Responsabilidad:
    Extraer una identidad estable y determinista de un RawSignalDetected
    para ser usada como clave de deduplicación (ProcessedSignal.fingerprint).

Principios de diseño:
- Función pura: no hace I/O, no tiene efectos secundarios.
- Determinista: el mismo post siempre genera el mismo fingerprint.
- Identidad estable: basada en IDs nativos o URLs canónicas — nunca en
  contenido mutable (caption, métricas, timestamps).
- Fallo explícito: si no existe ninguna identidad estable, lanza
  FingerprintError en lugar de generar un fingerprint basura.

Estrategia por fuente (source):

    instagram:
        Prioridad 1 — ID nativo del post:
            raw_payload["data"]["id"] o raw_payload["data"]["shortCode"]
            Formato: "instagram:<native_id>"
            Razón: el ID de Instagram es inmutable aunque el caption o las
            métricas cambien. Garantiza deduplicación exacta.

        Prioridad 2 (fallback) — URL canónica sin query params:
            raw_payload["url_queried"], limpiando tracking params.
            Formato: "instagram:<clean_url>"
            Razón: la URL del post es estable aunque cambie el shortCode
            en algunos edge cases de Apify.

        Fallo: excepción explícita si ninguna identidad está disponible.

    Fuentes genéricas (manual_input, etc.):
        Campo explícito raw_payload["fingerprint_id"].
        Formato: "<source>:<fingerprint_id>"
        Si no existe, lanza FingerprintError.

Prohibiciones explícitas:
    - NO usar hash del caption/content: mutable, rompe deduplicación.
    - NO usar métricas (likes, reach): cambian con el tiempo.
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
        str: Fingerprint en formato "<source>:<identity>".
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
    Estrategia de fingerprint para señales de fuente 'instagram'.

    Prioridad 1: ID nativo del post (raw_payload["data"]["id"])
    Prioridad 2: URL canónica sin query params (raw_payload["url_queried"])

    Raises:
        FingerprintError: Si ninguna identidad estable está disponible.
    """
    # Prioridad 1: ID nativo del post de Instagram
    data = payload.get("data")
    if isinstance(data, dict):
        # Apify puede exponer el ID como "id" o "shortCode" según el endpoint
        native_id = data.get("id") or data.get("shortCode")
        if isinstance(native_id, str) and native_id.strip():
            return f"instagram:{native_id.strip()}"

    # Prioridad 2 (fallback): URL canónica sin query params de tracking
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
