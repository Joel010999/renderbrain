from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from runtime.contracts.knowledge import MissionIntelligenceView, InsightSummary
from runtime.api.contracts import PatternResponse, OpportunityResponse
from runtime.api.dependencies import get_mission_repo, get_knowledge_repo, get_retriever
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
from runtime.engines.cognitive.retriever import KnowledgeContextRetriever

router = APIRouter(prefix="/missions/{mission_id}", tags=["Intelligence"])


async def _ensure_mission_exists(mission_id: UUID, repo: MissionRepository) -> None:
    mission = await repo.get_by_id(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")


@router.get("/intelligence", response_model=MissionIntelligenceView)
async def get_mission_intelligence(
    mission_id: UUID,
    insight_limit: int = Query(20, le=100),
    pattern_limit: int = Query(10, le=100),
    opportunity_limit: int = Query(5, le=100),
    mission_repo: MissionRepository = Depends(get_mission_repo),
    retriever: KnowledgeContextRetriever = Depends(get_retriever)
):
    """
    Recupera la vista unificada de inteligencia de la misión (Insights, Patterns, Opportunities).
    """
    await _ensure_mission_exists(mission_id, mission_repo)
    return await retriever.retrieve(
        mission_id=mission_id,
        insight_limit=insight_limit,
        pattern_limit=pattern_limit,
        opportunity_limit=opportunity_limit
    )


@router.get("/insights", response_model=List[InsightSummary])
async def list_insights(
    mission_id: UUID,
    limit: int = Query(100, le=1000),
    mission_repo: MissionRepository = Depends(get_mission_repo),
    knowledge_repo: KnowledgeCoreRepository = Depends(get_knowledge_repo)
):
    """
    Recupera los insights crudos detectados para esta misión en orden cronológico inverso.
    """
    await _ensure_mission_exists(mission_id, mission_repo)
    insights = await knowledge_repo.list_recent_insights(mission_id, limit=limit)
    return insights


@router.get("/patterns", response_model=List[PatternResponse])
async def list_patterns(
    mission_id: UUID,
    limit: int = Query(100, le=1000),
    mission_repo: MissionRepository = Depends(get_mission_repo),
    knowledge_repo: KnowledgeCoreRepository = Depends(get_knowledge_repo)
):
    """
    Recupera los patrones detectados para esta misión.
    Incluye los IDs de los insights en los que se apoyan para trazabilidad.
    """
    await _ensure_mission_exists(mission_id, mission_repo)
    models = await knowledge_repo.list_patterns_with_support(mission_id, limit=limit)
    return [
        PatternResponse(
            id=p.id,
            content=p.content,
            confidence=p.confidence,
            support_count=p.support_count,
            created_at=p.created_at,
            supporting_insight_ids=support_ids
        )
        for p, support_ids in models
    ]


@router.get("/opportunities", response_model=List[OpportunityResponse])
async def list_opportunities(
    mission_id: UUID,
    limit: int = Query(100, le=1000),
    mission_repo: MissionRepository = Depends(get_mission_repo),
    knowledge_repo: KnowledgeCoreRepository = Depends(get_knowledge_repo)
):
    """
    Recupera las oportunidades estratégicas detectadas para esta misión.
    Incluye los IDs de los patrones en los que se apoyan para trazabilidad.
    """
    await _ensure_mission_exists(mission_id, mission_repo)
    models = await knowledge_repo.list_opportunities_with_support(mission_id, limit=limit)
    return [
        OpportunityResponse(
            id=o.id,
            content=o.content,
            confidence=o.confidence,
            created_at=o.created_at,
            supporting_pattern_ids=support_ids
        )
        for o, support_ids in models
    ]
