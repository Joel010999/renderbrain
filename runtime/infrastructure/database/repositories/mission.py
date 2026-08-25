"""
runtime/infrastructure/database/repositories/mission.py

Repositorio para la gestión operacional de Missions.

A1.1 — Actualizado para incluir target_type, observation_scope, story_interval_seconds.
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
            target_type=mission.target_type,
            observation_scope=mission.observation_scope,
            story_interval_seconds=mission.story_interval_seconds,
            enabled=mission.enabled,
            interval_seconds=mission.interval_seconds,
            created_at=mission.created_at,
            updated_at=mission.updated_at,
        )
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

        return self._model_to_domain(model)

    async def list_all(self, enabled_only: bool = False) -> list[Mission]:
        """Devuelve las misiones, con opción a filtrar sólo habilitadas."""
        stmt = select(MissionModel)
        if enabled_only:
            stmt = stmt.where(MissionModel.enabled.is_(True))

        result = await self._session.execute(stmt)
        models = result.scalars().all()

        return [self._model_to_domain(m) for m in models]

    async def list_enabled(self) -> list[Mission]:
        """Devuelve todas las Missions que están habilitadas."""
        return await self.list_all(enabled_only=True)

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------

    @staticmethod
    def _model_to_domain(m: MissionModel) -> Mission:
        """Mapea MissionModel (ORM) → Mission (Pydantic)."""
        return Mission(
            id=m.id,
            name=m.name,
            source=m.source,
            target=m.target,
            target_type=m.target_type or "post",
            observation_scope=m.observation_scope,
            story_interval_seconds=m.story_interval_seconds,
            enabled=m.enabled,
            interval_seconds=m.interval_seconds,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )
