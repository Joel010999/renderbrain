"""
runtime/infrastructure/database/repositories/canonical_signal.py

CanonicalSignalRepository — persistencia asíncrona de CanonicalSignal.

Responsabilidades:
    save(signal)        → persiste un CanonicalSignal (contrato Pydantic).
    get_by_id(id)       → devuelve CanonicalSignal | None (contrato Pydantic).

Principios de diseño:
- El repositorio recibe y devuelve el contrato Pydantic CanonicalSignal.
  El modelo ORM CanonicalSignalModel nunca se expone fuera de esta capa.
- El mapeo CanonicalSignal ↔ CanonicalSignalModel está encapsulado en los
  métodos privados _to_orm() y _to_domain().
- Inmutabilidad: NO hay update(), delete(), listados ni filtros.
- La sesión se inyecta en el constructor para facilitar el testing y
  mantener la gestión del ciclo de vida fuera del repositorio.
- Si get_by_id() no encuentra la fila, devuelve None (sin excepciones
  de dominio en este MVP).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel


class CanonicalSignalRepository:
    """
    Repositorio asíncrono de solo-escritura/lectura para CanonicalSignal.

    Args:
        session: AsyncSession activa. El llamador gestiona commit/rollback
                 y el ciclo de vida de la sesión.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    async def save(self, signal: CanonicalSignal) -> None:
        """
        Persiste un CanonicalSignal en la tabla canonical_signals.

        Mapea el contrato Pydantic al modelo ORM y lo agrega a la sesión.
        El commit es responsabilidad del llamador (o del context-manager
        get_session() de la infraestructura).

        Args:
            signal: CanonicalSignal (contrato Pydantic) a persistir.
        """
        orm_model = self._to_orm(signal)
        self._session.add(orm_model)
        await self._session.flush()

    async def get_by_id(self, signal_id: UUID) -> CanonicalSignal | None:
        """
        Recupera un CanonicalSignal por su ID.

        Args:
            signal_id: UUID del CanonicalSignal a recuperar.

        Returns:
            CanonicalSignal si existe, None si no se encuentra.
        """
        stmt = select(CanonicalSignalModel).where(
            CanonicalSignalModel.id == signal_id
        )
        result = await self._session.execute(stmt)
        orm_model = result.scalar_one_or_none()

        if orm_model is None:
            return None

        return self._to_domain(orm_model)

    # ------------------------------------------------------------------
    # Mapeo privado — encapsulado en la capa de persistencia
    # ------------------------------------------------------------------

    @staticmethod
    def _to_orm(signal: CanonicalSignal) -> CanonicalSignalModel:
        """Mapea CanonicalSignal (Pydantic) → CanonicalSignalModel (ORM)."""
        return CanonicalSignalModel(
            id=signal.id,
            mission_id=signal.mission_id,
            source_event_id=signal.source_event_id,
            source=signal.source,
            sensor=signal.sensor,
            content=signal.content,
            author=signal.author,
            language=signal.language,
            metrics=signal.metrics,
            captured_at=signal.captured_at,
            normalized_at=signal.normalized_at,
        )

    @staticmethod
    def _to_domain(model: CanonicalSignalModel) -> CanonicalSignal:
        """Mapea CanonicalSignalModel (ORM) → CanonicalSignal (Pydantic)."""
        return CanonicalSignal(
            id=model.id,
            mission_id=model.mission_id,
            source_event_id=model.source_event_id,
            source=model.source,
            sensor=model.sensor,
            content=model.content,
            author=model.author,
            language=model.language,
            metrics=model.metrics,
            captured_at=model.captured_at,
            normalized_at=model.normalized_at,
        )
