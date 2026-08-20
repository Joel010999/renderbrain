"""
runtime/infrastructure/database/repositories/processed_signal.py

Repositorio para la deduplicación de señales procesadas (S4.1).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.contracts.processed_signal import ProcessedSignal
from runtime.infrastructure.database.models.mission import ProcessedSignalModel


class ProcessedSignalRepository:
    """Manejo de registros de deduplicación (ProcessedSignal)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, processed_signal: ProcessedSignal) -> ProcessedSignal:
        """
        Registra un fingerprint como procesado.
        Utiliza session.add() + session.flush().
        Si viola el constraint UNIQUE(mission_id, source, fingerprint),
        lanzará una IntegrityError de SQLAlchemy.
        """
        model = ProcessedSignalModel(
            id=processed_signal.id,
            mission_id=processed_signal.mission_id,
            source=processed_signal.source,
            fingerprint=processed_signal.fingerprint,
            processed_at=processed_signal.processed_at,
        )
        self._session.add(model)
        await self._session.flush()
        return processed_signal

    async def exists(self, mission_id: UUID, source: str, fingerprint: str) -> bool:
        """Comprueba si un fingerprint específico ya fue procesado para esta misión y fuente."""
        result = await self._session.execute(
            select(ProcessedSignalModel.id).where(
                ProcessedSignalModel.mission_id == mission_id,
                ProcessedSignalModel.source == source,
                ProcessedSignalModel.fingerprint == fingerprint,
            )
        )
        return result.scalar_one_or_none() is not None
