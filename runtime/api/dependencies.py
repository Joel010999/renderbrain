from typing import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.infrastructure.database.session import get_session
from runtime.infrastructure.database.repositories.mission import MissionRepository
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
from runtime.engines.cognitive.retriever import KnowledgeContextRetriever

async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provee una sesión asíncrona de la base de datos."""
    async for session in get_session():
        yield session

async def get_mission_repo(session: AsyncSession = Depends(get_db_session)) -> MissionRepository:
    """Provee el repositorio de misiones."""
    return MissionRepository(session)

async def get_knowledge_repo(session: AsyncSession = Depends(get_db_session)) -> KnowledgeCoreRepository:
    """Provee el repositorio principal de conocimiento."""
    return KnowledgeCoreRepository(session)

async def get_retriever(repo: KnowledgeCoreRepository = Depends(get_knowledge_repo)) -> KnowledgeContextRetriever:
    """Provee el retriever determinista de la vista de inteligencia."""
    return KnowledgeContextRetriever(repo)
