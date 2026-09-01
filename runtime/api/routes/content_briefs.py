"""
runtime/api/routes/content_briefs.py

Endpoints de lectura para ContentBriefs — Agent 3.

Endpoints:
    GET /api/v1/missions/{mission_id}/content-briefs
        Lista los ContentBriefs de una misión (más recientes primero).

    GET /api/v1/content-briefs/{id}
        Recupera un ContentBrief por su UUID.

Restricciones:
    - Solo lectura (GET). Escritura es responsabilidad exclusiva del Agent 3.
    - Autenticación: heredada del router padre (get_current_admin).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from runtime.api.contracts import ContentBriefResponse
from runtime.api.dependencies import get_content_brief_repo, get_mission_repo
from runtime.infrastructure.database.repositories.content_brief import ContentBriefRepository
from runtime.infrastructure.database.repositories.mission import MissionRepository

router = APIRouter(tags=["Content Briefs"])


async def _ensure_mission_exists(mission_id: UUID, repo: MissionRepository) -> None:
    mission = await repo.get_by_id(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")


@router.get(
    "/missions/{mission_id}/content-briefs",
    response_model=list[ContentBriefResponse],
)
async def list_content_briefs(
    mission_id: UUID,
    limit: int = Query(20, le=100),
    mission_repo: MissionRepository = Depends(get_mission_repo),
    brief_repo: ContentBriefRepository = Depends(get_content_brief_repo),
):
    """
    Lista los ContentBriefs generados para una misión.

    Ordenados del más reciente al más antiguo.
    Incluye todos los campos del ContentBrief para trazabilidad completa.
    """
    await _ensure_mission_exists(mission_id, mission_repo)
    briefs = await brief_repo.list_by_mission(mission_id=mission_id, limit=limit)
    return [ContentBriefResponse.from_brief(b) for b in briefs]


@router.get(
    "/content-briefs/{brief_id}",
    response_model=ContentBriefResponse,
)
async def get_content_brief(
    brief_id: UUID,
    brief_repo: ContentBriefRepository = Depends(get_content_brief_repo),
):
    """
    Recupera un ContentBrief por su UUID primario.

    Permite acceder a un brief específico y verificar su trazabilidad
    hacia la Opportunity (opportunity_id) → Patterns → Insights → Signals.
    """
    brief = await brief_repo.get_by_id(brief_id)
    if not brief:
        raise HTTPException(status_code=404, detail="ContentBrief not found")
    return ContentBriefResponse.from_brief(brief)
