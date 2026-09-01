"""
runtime/orchestration/content_strategy_flow.py

Orquestador del flujo de estrategia de contenido — Agent 3.

Responsabilidades:
    - Llamar al ContentStrategist para generar el ContentBrief.
    - Persistir el brief con aislamiento transaccional total respecto al Agent 2.
    - Garantizar que ningún fallo del Agent 3 afecte la Opportunity del Agent 2.

Aislamiento transaccional:
    run_content_strategy_flow() abre su PROPIA async_session.
    Nunca recibe la sesión del Agent 2 como parámetro.
    El commit del Agent 2 ya ocurrió ANTES de que esta función sea llamada.

Manejo de errores:
    - InvalidContentBriefOutputError: error semántico → loguear + retornar None.
    - LLMProviderError / RuntimeError: error de infra → loguear + retornar None.
    - SQLAlchemy errors: error de persistencia → loguear + retornar None.

    NINGÚN error sube al SignalWorker de forma no controlada desde esta función.
    La Opportunity del Agent 2 queda siempre intacta.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from runtime.contracts.content_brief import ContentBrief
from runtime.contracts.knowledge import Opportunity
from runtime.engines.content.strategist import (
    ContentStrategist, 
    InvalidContentBriefOutputError, 
    MisalignedBrandBriefError
)
from runtime.infrastructure.database.models.mission import MissionModel
from runtime.infrastructure.database.repositories.content_brief import ContentBriefRepository
from runtime.infrastructure.llm.interfaces import LLMProvider
from runtime.shared.logger import get_logger
from runtime.contracts.brand import BRAND_CONTEXT

logger: logging.Logger = get_logger(__name__)


async def run_content_strategy_flow(
    opportunity: Opportunity,
    mission_context: str,
    llm_provider: LLMProvider,
    session_factory: async_sessionmaker[AsyncSession],
) -> ContentBrief | None:
    """
    Genera y persiste un ContentBrief para la Opportunity dada.

    Se ejecuta en su propia transacción, completamente aislada del Agent 2.
    Todos los errores son capturados internamente; nunca se propagan al caller.

    Args:
        opportunity:     Opportunity ya committeada por el Agent 2.
        mission_context: Contexto de misión (nombre, scope, target).
        llm_provider:    LLMProvider inyectado (mismo que usa el worker).
        session_factory: Fábrica de sesiones async (misma que usa el worker).

    Returns:
        ContentBrief si la generación y persistencia fueron exitosas.
        None si ocurrió cualquier error (loguado internamente).
    """
    logger.info(
        "Agent 3: content strategy flow started",
        extra={
            "opportunity_id": str(opportunity.id),
            "opportunity_title": opportunity.title,
            "mission_id": str(opportunity.mission_id),
        },
    )

    # 1. Fetch Mission scope and generate ContentBrief via LLM
    try:
        async with session_factory() as session:
            mission = await session.get(MissionModel, opportunity.mission_id)
            if not mission:
                logger.error("Agent 3: Mission not found", extra={"mission_id": str(opportunity.mission_id)})
                return None
            
            observation_scope = mission.observation_scope or "reference"
            
        strategist = ContentStrategist(llm_provider=llm_provider)
        brief = await strategist.generate(
            opportunity=opportunity,
            mission_context=mission_context,
            observation_scope=observation_scope,
            brand_context=BRAND_CONTEXT,
        )
    except (InvalidContentBriefOutputError, MisalignedBrandBriefError) as e:
        logger.warning(
            "Agent 3: semantic/alignment error — discarding brief, opportunity intact",
            extra={
                "opportunity_id": str(opportunity.id),
                "error": str(e),
            },
        )
        try:
            async with session_factory() as session:
                from sqlalchemy import update
                from runtime.infrastructure.database.models.knowledge import OpportunityModel
                stmt = (
                    update(OpportunityModel)
                    .where(OpportunityModel.id == opportunity.id)
                    .values(content_generation_attempts=OpportunityModel.content_generation_attempts + 1)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception as update_err:
            logger.error("Failed to increment content_generation_attempts", exc_info=update_err)
        return None
    except Exception as e:
        logger.error(
            "Agent 3: provider/infra error during content brief generation",
            extra={
                "opportunity_id": str(opportunity.id),
                "error": str(e),
            },
            exc_info=True,
        )
        return None

    # 2. Persistir el brief en transacción propia (aislada del Agent 2)
    try:
        async with session_factory() as session:
            repo = ContentBriefRepository(session)
            inserted = await repo.save_if_not_exists(brief)
            await session.commit()

        if inserted:
            logger.info(
                "Agent 3: content brief persisted",
                extra={
                    "brief_id": str(brief.id),
                    "opportunity_id": str(opportunity.id),
                    "content_format": brief.content_format.value,
                    "objective": brief.objective.value,
                },
            )
            try:
                async with session_factory() as update_session:
                    from sqlalchemy import update
                    from runtime.infrastructure.database.models.knowledge import OpportunityModel
                    stmt = (
                        update(OpportunityModel)
                        .where(OpportunityModel.id == opportunity.id)
                        .values(content_generation_attempts=0)
                    )
                    await update_session.execute(stmt)
                    await update_session.commit()
            except Exception as update_err:
                logger.error("Failed to reset content_generation_attempts", exc_info=update_err)
        else:
            logger.info(
                "Agent 3: content brief already exists for this opportunity — skipping",
                extra={
                    "opportunity_id": str(opportunity.id),
                },
            )
    except Exception as e:
        logger.error(
            "Agent 3: error persisting content brief",
            extra={
                "opportunity_id": str(opportunity.id),
                "brief_id": str(brief.id),
                "error": str(e),
            },
            exc_info=True,
        )
        return None

    return brief
