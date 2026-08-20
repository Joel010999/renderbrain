"""
Test de integración S1.3 — CanonicalSignalRepository contra PostgreSQL real.

Flujo verificado:
    CanonicalSignal (Pydantic) → repository.save() → PostgreSQL
    repository.get_by_id()     → CanonicalSignal equivalente

Requisito previo: contenedor renderbrain-postgres corriendo y healthy.

    docker compose up -d
    uv run pytest tests/integration/test_canonical_signal_repository.py -v -m integration

Casos verificados:
    1. save() + get_by_id() con todos los campos (incluyendo opcionales).
    2. Equivalencia exacta campo a campo entre original y recuperado.
    3. get_by_id() con ID inexistente devuelve None.

Limpieza: DELETE explícito en el finally de cada test — la tabla no se trunca.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete

from runtime.contracts import CanonicalSignal
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.repositories import CanonicalSignalRepository


@pytest.mark.integration
async def test_repository_save_and_get_by_id_full_fields():
    """
    Ciclo completo: save() → get_by_id() con todos los campos (incluyendo opcionales).

    Verifica:
      ✓ id, mission_id, source_event_id, source, sensor
      ✓ content, author, language
      ✓ metrics (dict con float e int)
      ✓ captured_at y normalized_at (timezone-aware en UTC)
      ✓ El objeto recuperado es una instancia de CanonicalSignal (Pydantic)
    """
    # ----------------------------------------------------------------
    # 1. Construir CanonicalSignal con todos los campos explícitos
    # ----------------------------------------------------------------
    fixed_captured_at = datetime(2026, 8, 12, 15, 0, 0, tzinfo=UTC)
    fixed_normalized_at = datetime(2026, 8, 12, 15, 1, 0, tzinfo=UTC)

    original = CanonicalSignal(
        mission_id=uuid4(),
        source_event_id=uuid4(),
        source="manual_input",
        sensor="manual",
        content="Señal canónica de prueba S1.3 — integración real.",
        author="RenderBrain Test Suite",
        language="es",
        metrics={"likes": 42, "reach": 1000, "score": 0.95},
        captured_at=fixed_captured_at,
        normalized_at=fixed_normalized_at,
    )

    async with async_session() as session:
        try:
            # ----------------------------------------------------------------
            # 2. Persistir
            # ----------------------------------------------------------------
            repo = CanonicalSignalRepository(session)
            await repo.save(original)
            await session.commit()

            # ----------------------------------------------------------------
            # 3. Recuperar
            # ----------------------------------------------------------------
            recovered = await repo.get_by_id(original.id)

            # ----------------------------------------------------------------
            # 4. Verificar que es un contrato Pydantic (no un ORM model)
            # ----------------------------------------------------------------
            assert isinstance(recovered, CanonicalSignal), (
                f"Se esperaba CanonicalSignal, obtenido: {type(recovered)}"
            )

            # ----------------------------------------------------------------
            # 5. Equivalencia campo a campo
            # ----------------------------------------------------------------
            assert recovered.id == original.id, (
                f"id difiere: {recovered.id!r}"
            )
            assert recovered.mission_id == original.mission_id, (
                f"mission_id difiere: {recovered.mission_id!r}"
            )
            assert recovered.source_event_id == original.source_event_id, (
                f"source_event_id difiere: {recovered.source_event_id!r}"
            )
            assert recovered.source == original.source, (
                f"source difiere: {recovered.source!r}"
            )
            assert recovered.sensor == original.sensor, (
                f"sensor difiere: {recovered.sensor!r}"
            )
            assert recovered.content == original.content, (
                f"content difiere: {recovered.content!r}"
            )
            assert recovered.author == original.author, (
                f"author difiere: {recovered.author!r}"
            )
            assert recovered.language == original.language, (
                f"language difiere: {recovered.language!r}"
            )
            assert recovered.metrics == original.metrics, (
                f"metrics difiere: {recovered.metrics!r}"
            )
            # Timestamps: comparar con timezone-aware
            assert recovered.captured_at == original.captured_at, (
                f"captured_at difiere: {recovered.captured_at!r}"
            )
            assert recovered.captured_at.tzinfo is not None, (
                "captured_at debe ser timezone-aware"
            )
            assert recovered.normalized_at == original.normalized_at, (
                f"normalized_at difiere: {recovered.normalized_at!r}"
            )
            assert recovered.normalized_at.tzinfo is not None, (
                "normalized_at debe ser timezone-aware"
            )

        finally:
            # Limpiar la fila exacta — sin truncar la tabla.
            # rollback primero para liberar la transacción anterior (si falló),
            # luego DELETE + commit en la misma sesión.
            await session.rollback()
            await session.execute(
                delete(CanonicalSignalModel).where(
                    CanonicalSignalModel.id == original.id
                )
            )
            await session.commit()


@pytest.mark.integration
async def test_repository_get_by_id_not_found_returns_none():
    """
    get_by_id() con un ID que no existe en la tabla devuelve None.

    Verifica que el repositorio no lanza excepciones de dominio en este MVP.
    """
    nonexistent_id: UUID = uuid4()

    async with async_session() as session:
        repo = CanonicalSignalRepository(session)
        result = await repo.get_by_id(nonexistent_id)

    assert result is None, (
        f"Se esperaba None para ID inexistente, obtenido: {result!r}"
    )
