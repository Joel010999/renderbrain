"""
runtime/infrastructure/database/repositories/mission.py

Repositorio para la gestión operacional de Missions (S4.1).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.contracts.mission import Mission
from runtime.infrastructure.database.models.mission import MissionModel


class MissionRepository:
    """Manejo de persistencia para la entidad Mission."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, mission: Mission) -> Mission:
        """
        Guarda o actualiza una Mission.
        Sigue la convención del proyecto: session.add() + session.flush().
        El commit físico es responsabilidad del orquestador o llamador.
        """
        model = MissionModel(
            id=mission.id,
            name=mission.name,
            source=mission.source,
            target=mission.target,
            enabled=mission.enabled,
            interval_seconds=mission.interval_seconds,
            created_at=mission.created_at,
            updated_at=mission.updated_at,
        )
        # Se asume inserción pura o merge si se necesita UPSERT en el futuro.
        # Por ahora, usamos merge para soportar creaciones idempotentes o updates.
        await self._session.merge(model)
        await self._session.flush()
        return mission

    async def get_by_id(self, mission_id: UUID) -> Mission | None:
        """Recupera una Mission por su ID."""
        result = await self._session.execute(
            select(MissionModel).where(MissionModel.id == mission_id)
        )
        model = result.scalar_one_or_none()
        if not model:
            return None

        return Mission(
            id=model.id,
            name=model.name,
            source=model.source,
            target=model.target,
            enabled=model.enabled,
            interval_seconds=model.interval_seconds,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def list_all(self, enabled_only: bool = False) -> list[Mission]:
        """Devuelve las misiones, con opción a filtrar sólo habilitadas."""
        stmt = select(MissionModel)
        if enabled_only:
            stmt = stmt.where(MissionModel.enabled.is_(True))
            
        result = await self._session.execute(stmt)
        models = result.scalars().all()

        return [
            Mission(
                id=m.id,
                name=m.name,
                source=m.source,
                target=m.target,
                enabled=m.enabled,
                interval_seconds=m.interval_seconds,
                created_at=m.created_at,
                updated_at=m.updated_at,
            )
            for m in models
        ]

    async def list_enabled(self) -> list[Mission]:
        """Devuelve todas las Missions que están habilitadas."""
        return await self.list_all(enabled_only=True)
