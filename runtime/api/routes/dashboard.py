"""
runtime/api/routes/dashboard.py

Dashboard para visualizar la inteligencia operativa.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from runtime.api.dependencies import get_mission_repo, get_retriever
from runtime.engines.cognitive.retriever import KnowledgeContextRetriever
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.contracts.knowledge import MissionIntelligenceView
from runtime.api.auth import get_current_admin

router = APIRouter(prefix="/dashboard", tags=["Dashboard"], dependencies=[Depends(get_current_admin)])

import os
templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
templates = Jinja2Templates(directory=templates_dir)

@router.get("", response_class=HTMLResponse)
async def dashboard_home(
    request: Request,
    repo: MissionRepository = Depends(get_mission_repo)
):
    """
    Página principal del dashboard con métricas y listado de misiones.
    """
    missions = await repo.list_all()
    enabled_missions = [m for m in missions if m.enabled]
    
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "missions": missions,
            "total_missions": len(missions),
            "enabled_missions": len(enabled_missions),
        }
    )

@router.get("/missions/{mission_id}", response_class=HTMLResponse)
async def dashboard_mission_detail(
    request: Request,
    mission_id: UUID,
    repo: MissionRepository = Depends(get_mission_repo),
    retriever: KnowledgeContextRetriever = Depends(get_retriever)
):
    """
    Vista detallada de la misión y su inteligencia (Insights, Patterns, Opportunities).
    """
    mission = await repo.get_by_id(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    intelligence: MissionIntelligenceView = await retriever.retrieve(mission_id)

    return templates.TemplateResponse(
        request=request,
        name="mission_detail.html",
        context={
            "mission": mission,
            "intelligence": intelligence,
            # A1.1 — campos de perfil para la vista de detalle
            "is_profile": mission.target_type == "profile",
            "observation_scope": mission.observation_scope,
            "story_interval_seconds": mission.story_interval_seconds,
        }
    )
