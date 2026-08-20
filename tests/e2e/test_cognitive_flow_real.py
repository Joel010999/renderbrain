"""
tests/e2e/test_cognitive_flow_real.py

Test E2E final del Sprint 3 (S3.4).
Demuestra el First Cognitive Flow orquestado usando OpenAI real y PostgreSQL real.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.models.knowledge import (
    EvidenceModel,
    InsightModel,
    KnowledgeTransactionModel,
)
from runtime.infrastructure.database.repositories.canonical_signal import CanonicalSignalRepository
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
from runtime.infrastructure.llm.openai import OpenAIAdapter
from runtime.orchestration.cognitive_flow import run_cognitive_flow
from runtime.shared.config import settings


@pytest.mark.external
@pytest.mark.asyncio
async def test_cognitive_flow_real_end_to_end():
    """
    Orquesta el flujo cognitivo completo sin usar Apify, 
    verificando la extracción de conocimiento por el LLM y su persistencia.
    """
    if not settings.OPENAI_API_KEY:
        pytest.skip("OPENAI_API_KEY no configurada — test externo omitido.")

    mission_id = uuid4()
    
    # 1. CanonicalSignal ficticia pero realista (conforme a contratos)
    signal = CanonicalSignal(
        mission_id=mission_id,
        source_event_id=uuid4(),
        source="test_system",
        sensor="test_manual_input",
        content="Todavía controlamos el stock con Excel y todas las semanas aparecen diferencias entre la planilla y el inventario real.",
        captured_at=datetime.now(UTC),
    )
    
    mission_context = "Detectar problemas operativos, procesos manuales, ineficiencias y necesidades de digitalización en empresas."
    
    # 2. Infraestructura y Engine (1 sola instancia)
    adapter = OpenAIAdapter()
    engine = CognitiveEngine(llm=adapter)
    
    async with async_session() as session:
        try:
            # 3. Preparación: Guardar CanonicalSignal en PostgreSQL
            sig_repo = CanonicalSignalRepository(session)
            await sig_repo.save(signal)
            await session.commit()
            
            # 4. Orquestación: Ejecutar flujo cognitivo (1 llamada a OpenAI, persistencia atómica)
            tx = await run_cognitive_flow(signal, mission_context, engine, session)
            
            # 5. Comprobar retornos
            assert tx is not None, "El LLM determinó que la señal es irrelevante, revisa el prompt o el modelo."
            assert tx.mission_id == mission_id
            
            # 6. Read back: Recuperar de PostgreSQL para asegurar consistencia e integración
            know_repo = KnowledgeCoreRepository(session)
            recovered = await know_repo.get_by_id(tx.id)
            
            assert recovered is not None
            
            # 7. Validar Genealogía (Requisito estricto S3.4)
            assert recovered.insight.evidence_id == recovered.evidence.id
            assert recovered.evidence.canonical_signal_id == signal.id
            assert recovered.mission_id == recovered.evidence.mission_id == recovered.insight.mission_id == signal.mission_id
            
            # 8. Validar Auditoría y contenido
            assert recovered.producer == "cognitive_engine"
            assert isinstance(recovered.reason, str) and len(recovered.reason) > 0
            assert isinstance(recovered.evidence.content, str) and len(recovered.evidence.content) > 0
            assert isinstance(recovered.insight.content, str) and len(recovered.insight.content) > 0

        finally:
            # 9. Limpieza de datos de prueba para mantener la BD limpia
            await session.rollback()
            await session.execute(
                delete(KnowledgeTransactionModel).where(KnowledgeTransactionModel.mission_id == mission_id)
            )
            await session.execute(
                delete(InsightModel).where(InsightModel.mission_id == mission_id)
            )
            await session.execute(
                delete(EvidenceModel).where(EvidenceModel.mission_id == mission_id)
            )
            await session.execute(
                delete(CanonicalSignalModel).where(CanonicalSignalModel.id == signal.id)
            )
            await session.commit()
