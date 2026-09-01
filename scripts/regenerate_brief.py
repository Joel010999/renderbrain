"""
scripts/regenerate_brief.py

Utilidad mínima para regenerar un ContentBrief existente.
Borra el ContentBrief actual para un opportunity_id específico y
luego invoca el run_content_strategy_flow para regenerarlo usando
el nuevo brand context.
"""

import argparse
import asyncio
import logging
import sys
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.content_brief import ContentBriefModel
from runtime.infrastructure.database.models.knowledge import OpportunityModel
from runtime.infrastructure.database.models.mission import MissionModel
from runtime.infrastructure.llm.openai import OpenAIAdapter
from runtime.orchestration.content_strategy_flow import run_content_strategy_flow
from runtime.contracts.knowledge import Opportunity
from runtime.shared.logger import get_logger

logger = get_logger(__name__)

async def regenerate_brief(opportunity_id: str) -> None:
    opp_uuid = UUID(opportunity_id)
    llm = OpenAIAdapter()
    
    async with async_session() as session:
        # 1. Fetch Opportunity
        opp_model = await session.get(OpportunityModel, opp_uuid)
        if not opp_model:
            logger.error("Opportunity no encontrada", extra={"opportunity_id": opportunity_id})
            return
            
        mission = await session.get(MissionModel, opp_model.mission_id)
        if not mission:
            logger.error("Misión no encontrada", extra={"mission_id": str(opp_model.mission_id)})
            return
            
        # 2. Borrar el ContentBrief actual
        stmt = delete(ContentBriefModel).where(ContentBriefModel.opportunity_id == opp_uuid)
        await session.execute(stmt)
        await session.commit()
        logger.info("ContentBrief anterior eliminado", extra={"opportunity_id": opportunity_id})
        
        # 3. Mapear a dominio
        opp = Opportunity(
            id=opp_model.id,
            mission_id=opp_model.mission_id,
            title=opp_model.title,
            description=opp_model.description,
            priority=opp_model.priority,
            created_at=opp_model.created_at,
        )
        
    # 4. Generar el nuevo Brief
    brief = await run_content_strategy_flow(
        opportunity=opp,
        mission_context=f"Analizar cuenta: {mission.target}",
        llm_provider=llm,
        session_factory=async_session,
    )
    
    if brief:
        logger.info("ContentBrief regenerado con éxito", extra={"brief_id": str(brief.id)})
        print("\n--- NUEVO CONTENT BRIEF ---")
        print(brief.model_dump_json(indent=2))
    else:
        logger.error("Falló la regeneración del ContentBrief")

def main():
    parser = argparse.ArgumentParser(description="Regenera un ContentBrief")
    parser.add_argument("opportunity_id", help="UUID de la Opportunity")
    args = parser.parse_args()
    
    asyncio.run(regenerate_brief(args.opportunity_id))

if __name__ == "__main__":
    main()
