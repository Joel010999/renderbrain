"""
Test de integración — Conexión real a Redis.

Requisito previo: contenedor renderbrain-redis corriendo y healthy.

    docker compose up -d
    uv run pytest tests/integration/test_redis_connection.py -v -m integration

Este test NO debe ejecutarse junto con los tests unitarios en CI/CD
a menos que haya un servicio Redis disponible.
"""

import pytest

from runtime.infrastructure.redis.client import get_redis_client


@pytest.mark.integration
async def test_ping_returns_pong():
    """
    Ejecuta PING contra el Redis configurado en REDIS_URL y verifica
    que la respuesta sea True o 'PONG'.

    Con decode_responses=True el cliente redis>=5.x devuelve True para PING.
    Versiones anteriores podían devolver b'PONG'; se tolera ambos.

    Valida que:
    - La URL de REDIS_URL es alcanzable desde la app.
    - El driver redis.asyncio establece conexión correctamente.
    - El servidor Redis está operativo y responde al comando PING.
    """
    client = get_redis_client()
    try:
        response = await client.ping()
        assert response in (True, "PONG"), (
            f"PING devolvió un valor inesperado: {response!r}"
        )
    finally:
        await client.aclose()


@pytest.mark.integration
async def test_set_and_get_roundtrip():
    """
    Escribe una clave temporal en Redis y la recupera inmediatamente
    para confirmar que las operaciones de lectura/escritura funcionan.

    La clave tiene TTL de 5 segundos para no dejar basura en la instancia.
    """
    client = get_redis_client()
    key = "renderbrain:test:roundtrip"
    expected_value = "c3.1-ok"

    try:
        await client.set(key, expected_value, ex=5)
        actual_value = await client.get(key)
        assert actual_value == expected_value, (
            f"Se esperaba '{expected_value}', se obtuvo: {actual_value!r}"
        )
    finally:
        # Limpiar aunque haya TTL, para buenas prácticas
        await client.delete(key)
        await client.aclose()
