"""
runtime/orchestration/cognitive_flow.py

Orquestador del flujo cognitivo (S3.4).
Coordina el paso de una CanonicalSignal por el CognitiveEngine 
y persiste la transacción resultante asegurando atomicidad.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.knowledge import KnowledgeTransaction, MissionIntelligenceView
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository


async def run_cognitive_flow(
    signal: CanonicalSignal,
    mission_context: str,
    cognitive_engine: CognitiveEngine,
    intelligence_view: "MissionIntelligenceView",
    session: AsyncSession,
) -> KnowledgeTransaction | None:
    """
    Ejecuta el flujo cognitivo para extraer conocimiento de una señal.
    Si la señal no es relevante, aborta limpiamente sin mutar estado.
    Si extrae conocimiento, lo persiste atómicamente (Unit of Work final).
    """
    # 1. Extracción vía motor cognitivo (LLM)
    transaction = await cognitive_engine.analyze(
        signal=signal, 
        mission_context=mission_context,
        knowledge_context=intelligence_view
    )
    
    if transaction is None:
        return None

    # 2. Construcción de Repositorio sobre la misma sesión
    repository = KnowledgeCoreRepository(session)

    # 3. Persistencia atómica (Flush)
    # Se delega el commit/rollback físico al llamador (ej. SignalWorker) para 
    # englobar Pattern Detection y ProcessedSignal en la misma transacción final.
    await repository.commit(transaction)
    
    return transaction
