from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.contracts.mission import Mission
from runtime.api.contracts import MissionCreateRequest, MissionUpdateRequest
from runtime.api.dependencies import get_mission_repo, get_db_session
from runtime.engines.sensors.factory import SUPPORTED_SENSOR_SOURCES
from runtime.infrastructure.database.repositories.mission import MissionRepository

router = APIRouter(prefix="/missions", tags=["Missions"])


@router.get("", response_model=List[Mission])
async def list_missions(
    enabled_only: bool = False,
    repo: MissionRepository = Depends(get_mission_repo)
):
    """
    Devuelve la lista de misiones operativas del sistema.
    """
    return await repo.list_all(enabled_only=enabled_only)


@router.get("/{mission_id}", response_model=Mission)
async def get_mission(
    mission_id: UUID,
    repo: MissionRepository = Depends(get_mission_repo)
):
    """
    Devuelve el detalle de una misión específica.
    """
    mission = await repo.get_by_id(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")
    return mission


@router.post("", response_model=Mission, status_code=201)
async def create_mission(
    request: MissionCreateRequest,
    repo: MissionRepository = Depends(get_mission_repo),
    session: AsyncSession = Depends(get_db_session)
):
    """
    Crea una nueva misión operativa.

    Soporta target_type='post' (URL de post individual) y
    target_type='profile' (perfil de Instagram para recolección diaria).

    Para target_type='profile':
    - interval_seconds default: 86400 (24h).
    - El target se normaliza automáticamente (acepta @user, URL, o username limpio).
    - observation_scope clasifica el propósito: competitor|inspiration|market|client|reference.
    """
    if request.source not in SUPPORTED_SENSOR_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported source. Supported sources: {list(SUPPORTED_SENSOR_SOURCES)}"
        )

    mission = Mission(
        name=request.name,
        source=request.source,
        target=request.target,
        target_type=request.target_type,
        observation_scope=request.observation_scope,
        interval_seconds=request.interval_seconds,
        story_interval_seconds=request.story_interval_seconds,
        enabled=request.enabled,
    )

    try:
        await repo.save(mission)
        await session.commit()
        return mission
    except Exception:
        await session.rollback()
        raise


@router.patch("/{mission_id}", response_model=Mission)
async def update_mission(
    mission_id: UUID,
    request: MissionUpdateRequest,
    repo: MissionRepository = Depends(get_mission_repo),
    session: AsyncSession = Depends(get_db_session)
):
    """
    Actualiza parcialmente una misión existente.
    Soporta edición de target_type, observation_scope y story_interval_seconds.
    """
    update_data = request.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=422, detail="At least one field must be provided for update.")

    if "source" in update_data and update_data["source"] not in SUPPORTED_SENSOR_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported source. Supported sources: {list(SUPPORTED_SENSOR_SOURCES)}"
        )

    mission = await repo.get_by_id(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    from datetime import datetime, timezone
    mission.updated_at = datetime.now(timezone.utc)

    for field, value in update_data.items():
        setattr(mission, field, value)

    try:
        await repo.save(mission)
        await session.commit()
        return mission
    except Exception:
        await session.rollback()
        raise


@router.post("/{mission_id}/enable", response_model=Mission)
async def enable_mission(
    mission_id: UUID,
    repo: MissionRepository = Depends(get_mission_repo),
    session: AsyncSession = Depends(get_db_session)
):
    """
    Habilita una misión para que el scheduler la ejecute.
    """
    mission = await repo.get_by_id(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    if not mission.enabled:
        from datetime import datetime, timezone
        mission.enabled = True
        mission.updated_at = datetime.now(timezone.utc)
        try:
            await repo.save(mission)
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    return mission


@router.post("/{mission_id}/disable", response_model=Mission)
async def disable_mission(
    mission_id: UUID,
    repo: MissionRepository = Depends(get_mission_repo),
    session: AsyncSession = Depends(get_db_session)
):
    """
    Deshabilita una misión.
    """
    mission = await repo.get_by_id(mission_id)
    if not mission:
        raise HTTPException(status_code=404, detail="Mission not found")

    if mission.enabled:
        from datetime import datetime, timezone
        mission.enabled = False
        mission.updated_at = datetime.now(timezone.utc)
        try:
            await repo.save(mission)
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    return mission
