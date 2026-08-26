"""
runtime/engines/cognitive/retriever.py

Implementación del KnowledgeContextRetriever (S5.1).
Se encarga de recuperar los insights recientes de la misma misión
y convertirlos en un KnowledgeContext listo para el Cognitive Engine.
"""

import asyncio
from uuid import UUID

from runtime.contracts.knowledge import InsightSummary, PatternSummary, OpportunitySummary, MissionIntelligenceView
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository


class KnowledgeContextRetriever:
    """
    Recuperador determinista de contexto de conocimiento.
    Utiliza el KnowledgeCoreRepository para obtener insights, patterns y opportunities recientes.
    """

    def __init__(self, repository: KnowledgeCoreRepository) -> None:
        self._repository = repository

    async def retrieve(
        self,
        mission_id: UUID,
        insight_limit: int = 20,
        pattern_limit: int = 10,
        opportunity_limit: int = 5,
    ) -> MissionIntelligenceView:
        """
        Recupera el MissionIntelligenceView asociado a una misión.
        Las consultas son independientes y aisladas por mission_id.
        
        Justificación de los límites:
        - insight_limit=20: Los insights son granulares y efímeros; se necesita una ventana amplia para encontrar recurrencias.
        - pattern_limit=10: Los patrones consolidan varios insights, por lo que son menos, pero aún suficientes para derivar oportunidades.
        - opportunity_limit=5: Las oportunidades son acciones altamente destiladas y estratégicas; un límite bajo evita diluir el foco de la misión.
        """
        # Ejecutamos las 3 queries de retrieval de forma concurrente
        recent_insights = await self._repository.list_recent_insights(mission_id=mission_id, limit=insight_limit)
        recent_patterns = await self._repository.list_recent_patterns(mission_id=mission_id, limit=pattern_limit)
        recent_opps = await self._repository.list_recent_opportunities(mission_id=mission_id, limit=opportunity_limit)

        insight_summaries = [
            InsightSummary(
                id=i.id, content=i.content, confidence=i.confidence, created_at=i.created_at
            )
            for i in recent_insights
        ]

        pattern_summaries = [
            PatternSummary(
                id=p.id,
                content=p.content,
                confidence=p.confidence,
                support_count=p.support_count,
                created_at=p.created_at,
            )
            for p in recent_patterns
        ]

        opp_summaries = [
            OpportunitySummary(
                id=o.id, title=o.title, description=o.description, priority=o.priority, confidence=o.confidence, created_at=o.created_at
            )
            for o in recent_opps
        ]

        return MissionIntelligenceView(
            mission_id=mission_id,
            insights=insight_summaries,
            patterns=pattern_summaries,
            opportunities=opp_summaries,
        )
