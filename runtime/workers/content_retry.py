"""
runtime/workers/content_retry.py

Tarea de background (retry/backfill) para Agent 3.

Busca Opportunities aprobadas que NO tienen un ContentBrief asociado (por fallos
semánticos del LLM, timeouts del provider, o porque son históricas previas a Agent 3).
Las reintenta de forma segura utilizando la idempotencia de run_content_strategy_flow.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from runtime.infrastructure.database.models.content_brief import ContentBriefModel
from runtime.infrastructure.database.models.knowledge import OpportunityModel
from runtime.infrastructure.llm.interfaces import LLMProvider
from runtime.orchestration.content_strategy_flow import run_content_strategy_flow
from runtime.shared.logger import get_logger

logger: logging.Logger = get_logger(__name__)


async def content_strategy_retry_loop(
    session_factory: async_sessionmaker[AsyncSession],
    llm_provider: LLMProvider,
    mission_context: str,
    stop_event: asyncio.Event,
    interval_seconds: int = 600,  # 10 minutes default
) -> None:
    """
    Loop en background que corre dentro del worker.
    Reintenta la generación de ContentBriefs para Opportunities que no lo tienen.
    """
    logger.info("Content Strategy Retry loop started", extra={"interval_seconds": interval_seconds})

    while not stop_event.is_set():
        try:
            await _process_missing_briefs(session_factory, llm_provider, mission_context)
        except Exception as e:
            logger.error("Error in Content Strategy Retry loop", extra={"error": str(e)}, exc_info=True)

        # Esperar el intervalo, interrumpible si el worker se apaga
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
            
    logger.info("Content Strategy Retry loop stopped")


async def _process_missing_briefs(
    session_factory: async_sessionmaker[AsyncSession],
    llm_provider: LLMProvider,
    mission_context: str,
) -> None:
    """Ejecuta una pasada buscando Opportunities huerfanas."""
    async with session_factory() as session:
        # LEFT JOIN para encontrar Opportunities sin ContentBrief
        stmt = (
            select(OpportunityModel)
            .outerjoin(ContentBriefModel, OpportunityModel.id == ContentBriefModel.opportunity_id)
            .where(ContentBriefModel.id == None)  # noqa: E711
            .order_by(OpportunityModel.created_at.desc())
            .limit(10)  # Procesar en batchs pequeños para no bloquear
        )
        result = await session.execute(stmt)
        opportunities = result.scalars().all()

    if not opportunities:
        return

    logger.info(
        "Content Strategy Retry: found opportunities without briefs",
        extra={"count": len(opportunities)},
    )

    for opp_model in opportunities:
        # Convertir OpportunityModel a contrato de dominio
        from runtime.contracts.knowledge import Opportunity
        
        opp = Opportunity(
            id=opp_model.id,
            mission_id=opp_model.mission_id,
            title=opp_model.title,
            description=opp_model.description,
            priority=opp_model.priority,
            created_at=opp_model.created_at,
        )

        # run_content_strategy_flow gestiona su propia transacción aislada
        await run_content_strategy_flow(
            opportunity=opp,
            mission_context=mission_context,
            llm_provider=llm_provider,
            session_factory=session_factory,
        )
