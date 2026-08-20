"""
runtime/infrastructure/database/probes.py

Función de comprobación de disponibilidad de PostgreSQL.

Reutiliza el engine global de session.py para ejecutar SELECT 1.
No expone URLs, credenciales ni mensajes de excepción internos —
solo retorna bool para que el endpoint /ready traduzca a HTTP.
"""

import logging

from sqlalchemy import text

from runtime.infrastructure.database.session import engine
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


async def check_postgres() -> bool:
    """
    Verifica la disponibilidad de PostgreSQL ejecutando SELECT 1.

    Usa el engine global (pool de conexiones existente) para no crear
    conexiones adicionales. La operación es mínima y de bajo coste.

    Returns:
        True  — PostgreSQL respondió correctamente.
        False — La conexión falló por cualquier motivo.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "PostgreSQL health check failed",
            extra={"error_type": type(exc).__name__},
        )
        return False
