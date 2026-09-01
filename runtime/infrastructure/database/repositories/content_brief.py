"""
runtime/infrastructure/database/repositories/content_brief.py

Repositorio append-only para ContentBrief — Agent 3.

Responsabilidades:
    - save_if_not_exists(): INSERT con ON CONFLICT DO NOTHING para idempotencia.
    - get_by_id(): Recuperar un ContentBrief por su UUID primario.
    - list_by_mission(): Listar ContentBriefs de una misión (para API).

Garantías:
    - Nunca lanza IntegrityError por duplicado de opportunity_id.
    - El llamador gestiona session.commit() / rollback().
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.contracts.content_brief import ContentBrief, ContentBriefSection
from runtime.infrastructure.database.models.content_brief import ContentBriefModel


class ContentBriefRepository:
    """
    Repositorio de escritura idempotente para ContentBrief.

    Usa INSERT ... ON CONFLICT DO NOTHING para garantizar que no se creen
    duplicados cuando el worker reinicia y vuelve a intentar generar el brief
    para la misma Opportunity.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_if_not_exists(self, brief: ContentBrief) -> bool:
        """
        Persiste un ContentBrief si no existe ya uno para el mismo opportunity_id.

        Usa INSERT ... ON CONFLICT DO NOTHING para idempotencia total.

        Returns:
            True si el brief fue insertado.
            False si ya existía un brief para este opportunity_id (no error).

        Note:
            No hace session.commit(). El llamador es responsable del commit.
        """
        sections_json = [
            {
                "order": sec.order,
                "title": sec.title,
                "content": sec.content,
            }
            for sec in brief.sections
        ]

        stmt = (
            pg_insert(ContentBriefModel)
            .values(
                id=brief.id,
                mission_id=brief.mission_id,
                opportunity_id=brief.opportunity_id,
                content_format=brief.content_format.value,
                objective=brief.objective.value,
                target_audience=brief.target_audience,
                angle=brief.angle.value,
                core_message=brief.core_message,
                hook=brief.hook,
                sections=sections_json,
                cta=brief.cta,
                visual_direction=brief.visual_direction,
                source_reasoning=brief.source_reasoning,
                status=brief.status,
                created_at=brief.created_at,
            )
            .on_conflict_do_nothing(index_elements=["opportunity_id"])
        )

        result = await self._session.execute(stmt)
        # rowcount == 1 → inserted, rowcount == 0 → conflict (already exists)
        inserted = result.rowcount == 1
        return inserted

    async def get_by_id(self, brief_id: UUID) -> Optional[ContentBrief]:
        """
        Recupera un ContentBrief por su ID primario.

        Returns:
            ContentBrief si existe, None en caso contrario.
        """
        stmt = select(ContentBriefModel).where(ContentBriefModel.id == brief_id)
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()

        if model is None:
            return None

        return self._to_contract(model)

    async def list_by_mission(
        self,
        mission_id: UUID,
        limit: int = 20,
    ) -> list[ContentBrief]:
        """
        Lista ContentBriefs de una misión en orden cronológico inverso.

        Args:
            mission_id: UUID de la misión.
            limit:      Máximo de resultados (default 20).

        Returns:
            Lista de ContentBrief, del más reciente al más antiguo.
        """
        stmt = (
            select(ContentBriefModel)
            .where(ContentBriefModel.mission_id == mission_id)
            .order_by(ContentBriefModel.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        return [self._to_contract(m) for m in models]

    @staticmethod
    def _to_contract(model: ContentBriefModel) -> ContentBrief:
        """Convierte un ContentBriefModel ORM a un ContentBrief de dominio."""
        from runtime.contracts.content_brief import ContentFormat, ContentObjective, ContentAngle

        sections_raw = model.sections or []
        sections = [
            ContentBriefSection(
                order=sec["order"],
                title=sec.get("title"),
                content=sec["content"],
            )
            for sec in sections_raw
        ]

        return ContentBrief(
            id=model.id,
            mission_id=model.mission_id,
            opportunity_id=model.opportunity_id,
            content_format=ContentFormat(model.content_format),
            objective=ContentObjective(model.objective),
            target_audience=model.target_audience,
            angle=ContentAngle(model.angle),
            core_message=model.core_message,
            hook=model.hook,
            sections=sections,
            cta=model.cta,
            visual_direction=model.visual_direction,
            source_reasoning=model.source_reasoning,
            status=model.status,
            created_at=model.created_at,
        )
