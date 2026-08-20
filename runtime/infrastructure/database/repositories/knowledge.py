"""
runtime/infrastructure/database/repositories/knowledge.py

Repositorio para el Knowledge Core — S3.1
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from runtime.contracts.knowledge import Evidence, Insight, KnowledgeTransaction, Pattern, Opportunity
from runtime.infrastructure.database.models.knowledge import (
    EvidenceModel,
    InsightModel,
    KnowledgeTransactionModel,
    PatternModel,
    OpportunityModel,
)


class KnowledgeCoreRepository:
    """
    Repositorio de escritura atómica para el Knowledge Core (S3.1).
    Solo provee operaciones append-only (sin update ni delete).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def commit(self, transaction: KnowledgeTransaction) -> None:
        """
        Persiste una KnowledgeTransaction de forma atómica.
        Incluye la inserción de Evidence, Insight y KnowledgeTransaction.
        Se espera que el llamador maneje el session.commit() / rollback().
        """
        # 1. Crear el modelo de Evidence
        evidence_model = EvidenceModel(
            id=transaction.evidence.id,
            mission_id=transaction.evidence.mission_id,
            canonical_signal_id=transaction.evidence.canonical_signal_id,
            content=transaction.evidence.content,
            confidence=transaction.evidence.confidence,
            created_at=transaction.evidence.created_at,
        )

        # 2. Crear el modelo de Insight
        insight_model = InsightModel(
            id=transaction.insight.id,
            mission_id=transaction.insight.mission_id,
            evidence_id=transaction.insight.evidence_id,
            content=transaction.insight.content,
            confidence=transaction.insight.confidence,
            created_at=transaction.insight.created_at,
        )

        # 3. Crear el modelo de Transacción
        tx_model = KnowledgeTransactionModel(
            id=transaction.id,
            mission_id=transaction.mission_id,
            action=transaction.action,
            evidence_id=transaction.evidence.id,
            insight_id=transaction.insight.id,
            producer=transaction.producer,
            reason=transaction.reason,
            created_at=transaction.created_at,
        )

        # Añadir al Identity Map de la sesión
        self._session.add(evidence_model)
        self._session.add(insight_model)
        self._session.add(tx_model)

        # Hacer un flush para que se inserten en la DB en la transacción actual, 
        # y así validar atomicidad si falla.
        await self._session.flush()

    async def get_by_id(self, transaction_id: UUID) -> Optional[KnowledgeTransaction]:
        """
        Recupera una KnowledgeTransaction completa por su ID.
        Usado estrictamente para read-back y tests en S3.1.
        """
        stmt = (
            select(KnowledgeTransactionModel)
            .where(KnowledgeTransactionModel.id == transaction_id)
            .options(
                joinedload(KnowledgeTransactionModel.evidence),
                joinedload(KnowledgeTransactionModel.insight),
            )
        )
        result = await self._session.execute(stmt)
        tx_model = result.unique().scalar_one_or_none()

        if tx_model is None:
            return None

        # Reconstruir contratos
        evidence = Evidence(
            id=tx_model.evidence.id,
            mission_id=tx_model.evidence.mission_id,
            canonical_signal_id=tx_model.evidence.canonical_signal_id,
            content=tx_model.evidence.content,
            confidence=tx_model.evidence.confidence,
            created_at=tx_model.evidence.created_at,
        )

        insight = Insight(
            id=tx_model.insight.id,
            mission_id=tx_model.insight.mission_id,
            evidence_id=tx_model.insight.evidence_id,
            content=tx_model.insight.content,
            confidence=tx_model.insight.confidence,
            created_at=tx_model.insight.created_at,
        )

        return KnowledgeTransaction(
            id=tx_model.id,
            mission_id=tx_model.mission_id,
            action=tx_model.action,
            evidence=evidence,
            insight=insight,
            producer=tx_model.producer,
            reason=tx_model.reason,
            created_at=tx_model.created_at,
        )

    async def list_recent_insights(self, mission_id: UUID, limit: int = 10) -> list[Insight]:
        """
        Recupera los insights más recientes de una misión, ordenados del más nuevo al más viejo.
        """
        stmt = (
            select(InsightModel)
            .where(InsightModel.mission_id == mission_id)
            .order_by(InsightModel.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        
        return [
            Insight(
                id=m.id,
                mission_id=m.mission_id,
                evidence_id=m.evidence_id,
                content=m.content,
                confidence=m.confidence,
                created_at=m.created_at,
            )
            for m in models
        ]

    async def add_pattern(self, pattern: Pattern, supporting_insight_ids: list[UUID]) -> Pattern:
        """
        Agrega un nuevo Pattern y lo relaciona con los Insights de soporte.
        Valida que los insights existan, pertenezcan a la misma mission, no haya duplicados
        y sean al menos 2.
        Solo hace add y flush, no commit.
        """
        if len(supporting_insight_ids) < 2:
            raise ValueError("Un Pattern requiere al menos 2 insights de soporte.")
            
        unique_ids = list(set(supporting_insight_ids))
        if len(unique_ids) < len(supporting_insight_ids):
            raise ValueError("Los IDs de los insights de soporte no deben estar duplicados.")

        # Obtener los modelos de insight
        stmt = select(InsightModel).where(InsightModel.id.in_(unique_ids))
        result = await self._session.execute(stmt)
        insight_models = list(result.scalars().all())

        if len(insight_models) != len(unique_ids):
            raise ValueError("Algunos insights de soporte no existen en la base de datos.")

        for im in insight_models:
            if im.mission_id != pattern.mission_id:
                raise ValueError("Todos los insights de soporte deben pertenecer a la misma misión que el Pattern.")

        pattern_model = PatternModel(
            id=pattern.id,
            mission_id=pattern.mission_id,
            content=pattern.content,
            confidence=pattern.confidence,
            support_count=pattern.support_count,
            created_at=pattern.created_at,
            insights=insight_models
        )
        
        self._session.add(pattern_model)
        await self._session.flush()
        return pattern

    async def list_recent_patterns(self, mission_id: UUID, limit: int = 10) -> list[Pattern]:
        """
        Recupera los patterns más recientes de una misión.
        """
        stmt = (
            select(PatternModel)
            .where(PatternModel.mission_id == mission_id)
            .order_by(PatternModel.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        
        return [
            Pattern(
                id=m.id,
                mission_id=m.mission_id,
                content=m.content,
                confidence=m.confidence,
                support_count=m.support_count,
                created_at=m.created_at,
            )
            for m in models
        ]

    async def add_opportunity(self, opportunity: Opportunity, supporting_pattern_ids: list[UUID]) -> Opportunity:
        """
        Agrega una nueva Opportunity y la relaciona con los Patterns de soporte.
        Valida que los patterns existan, pertenezcan a la misma mission, no haya duplicados
        y sea al menos 1.
        Solo hace add y flush, no commit.
        """
        if len(supporting_pattern_ids) < 1:
            raise ValueError("Una Opportunity requiere al menos 1 pattern de soporte.")
            
        unique_ids = list(set(supporting_pattern_ids))
        if len(unique_ids) < len(supporting_pattern_ids):
            raise ValueError("Los IDs de los patterns de soporte no deben estar duplicados.")

        # Obtener los modelos de pattern
        stmt = select(PatternModel).where(PatternModel.id.in_(unique_ids))
        result = await self._session.execute(stmt)
        pattern_models = list(result.scalars().all())

        if len(pattern_models) != len(unique_ids):
            raise ValueError("Algunos patterns de soporte no existen en la base de datos.")

        for pm in pattern_models:
            if pm.mission_id != opportunity.mission_id:
                raise ValueError("Todos los patterns de soporte deben pertenecer a la misma misión que la Opportunity.")

        opportunity_model = OpportunityModel(
            id=opportunity.id,
            mission_id=opportunity.mission_id,
            content=opportunity.content,
            confidence=opportunity.confidence,
            created_at=opportunity.created_at,
            patterns=pattern_models
        )
        
        self._session.add(opportunity_model)
        await self._session.flush()
        return opportunity

    async def list_recent_opportunities(self, mission_id: UUID, limit: int = 10) -> list[Opportunity]:
        """
        Recupera las opportunities más recientes de una misión.
        """
        stmt = (
            select(OpportunityModel)
            .where(OpportunityModel.mission_id == mission_id)
            .order_by(OpportunityModel.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        
        return [
            Opportunity(
                id=m.id,
                mission_id=m.mission_id,
                content=m.content,
                confidence=m.confidence,
                created_at=m.created_at,
            )
            for m in models
        ]

    async def list_patterns_with_support(self, mission_id: UUID, limit: int = 100) -> list[tuple[Pattern, list[UUID]]]:
        """
        Recupera patterns con su lista de IDs de insights de soporte.
        """
        from sqlalchemy.orm import selectinload
        stmt = (
            select(PatternModel)
            .options(selectinload(PatternModel.insights))
            .where(PatternModel.mission_id == mission_id)
            .order_by(PatternModel.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        
        return [
            (
                Pattern(
                    id=m.id,
                    mission_id=m.mission_id,
                    content=m.content,
                    confidence=m.confidence,
                    support_count=m.support_count,
                    created_at=m.created_at,
                ),
                [i.id for i in m.insights]
            )
            for m in models
        ]

    async def list_opportunities_with_support(self, mission_id: UUID, limit: int = 100) -> list[tuple[Opportunity, list[UUID]]]:
        """
        Recupera opportunities con su lista de IDs de patterns de soporte.
        """
        from sqlalchemy.orm import selectinload
        stmt = (
            select(OpportunityModel)
            .options(selectinload(OpportunityModel.patterns))
            .where(OpportunityModel.mission_id == mission_id)
            .order_by(OpportunityModel.created_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        models = result.scalars().all()
        
        return [
            (
                Opportunity(
                    id=m.id,
                    mission_id=m.mission_id,
                    content=m.content,
                    confidence=m.confidence,
                    created_at=m.created_at,
                ),
                [p.id for p in m.patterns]
            )
            for m in models
        ]

