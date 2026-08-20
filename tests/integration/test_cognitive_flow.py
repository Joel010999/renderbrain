"""
tests/integration/test_cognitive_flow.py

Tests offline para la orquestación del flujo cognitivo (S3.4).
Utiliza PostgreSQL real pero un LLM mockeado (FakeLLMProvider).
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.knowledge import MissionIntelligenceView
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.knowledge import EvidenceModel, KnowledgeTransactionModel, InsightModel
from runtime.infrastructure.database.repositories.canonical_signal import CanonicalSignalRepository
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
from runtime.orchestration.cognitive_flow import run_cognitive_flow
from tests.fakes.fake_llm_provider import FakeLLMProvider


@pytest.fixture
def base_signal() -> CanonicalSignal:
    """Provee una señal canónica válida y determinista."""
    return CanonicalSignal(
        mission_id=uuid4(),
        source_event_id=uuid4(),
        source="test_source",
        sensor="test_sensor",
        content="Test content for cognitive flow.",
        captured_at=datetime.now(UTC),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_cognitive_flow_relevant_signal(base_signal: CanonicalSignal):
    """
    Escenario A: Señal relevante.
    Verifica que el flujo extrae conocimiento, lo persiste atómicamente
    y puede ser recuperado con todas las FKs y relaciones intactas.
    """
    fake_response = json.dumps({
        "relevant": True,
        "evidence": "Evidence successfully extracted",
        "insight": "Insight strictly deduced",
        "confidence": 0.95,
        "reason": "Testing successful flow"
    })
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)

    async with async_session() as session:
        # 1. Guardar la señal base para cumplir la Foreign Key
        sig_repo = CanonicalSignalRepository(session)
        await sig_repo.save(base_signal)
        await session.commit()

        # 2. Ejecutar el flujo cognitivo completo
        mission_context = "Contexto de prueba"
        empty_view = MissionIntelligenceView(mission_id=base_signal.mission_id)
        tx = await run_cognitive_flow(base_signal, mission_context, engine, empty_view, session)
        await session.commit()

        # 3. Comprobar retornos en memoria
        assert tx is not None
        assert tx.id is not None
        assert tx.mission_id == base_signal.mission_id
        assert tx.producer == "cognitive_engine"
        assert tx.reason == "Testing successful flow"
        assert tx.evidence.content == "Evidence successfully extracted"
        assert tx.insight.content == "Insight strictly deduced"
        assert tx.evidence.canonical_signal_id == base_signal.id
        assert tx.insight.evidence_id == tx.evidence.id

        # 4. Leer de vuelta de PostgreSQL para comprobar que el session.commit() del orquestador funcionó
        know_repo = KnowledgeCoreRepository(session)
        recovered_tx = await know_repo.get_by_id(tx.id)

        assert recovered_tx is not None
        assert recovered_tx.id == tx.id
        assert recovered_tx.evidence.content == "Evidence successfully extracted"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_cognitive_flow_irrelevant_signal(base_signal: CanonicalSignal):
    """
    Escenario B: Señal irrelevante.
    Verifica que retorna None y no persiste ningún objeto fantasma.
    """
    fake_response = json.dumps({
        "relevant": False,
        "evidence": None,
        "insight": None,
        "confidence": None,
        "reason": "Not related"
    })
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)

    async with async_session() as session:
        # Guardar señal
        sig_repo = CanonicalSignalRepository(session)
        await sig_repo.save(base_signal)
        await session.commit()

        # Ejecutar orquestador
        empty_view = MissionIntelligenceView(mission_id=base_signal.mission_id)
        tx = await run_cognitive_flow(base_signal, "Contexto", engine, empty_view, session)

        # Verificar retorno None
        assert tx is None

        # Verificar que no se insertó evidencia alguna en esta sesión
        result = await session.execute(
            select(EvidenceModel).where(EvidenceModel.mission_id == base_signal.mission_id)
        )
        assert len(result.scalars().all()) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_cognitive_flow_rollback_on_error(base_signal: CanonicalSignal):
    """
    Escenario C: Rollback completo.
    Fuerza un fallo de BD (FK no existente) simulando caída en la fase de persistencia.
    Comprueba que el orquestador hace session.rollback() y levanta el error.
    """
    fake_response = json.dumps({
        "relevant": True,
        "evidence": "Evidence that should not be saved",
        "insight": "Insight that should not be saved",
        "confidence": 0.8,
        "reason": "Testing rollback"
    })
    llm = FakeLLMProvider(fake_response)
    engine = CognitiveEngine(llm)

    async with async_session() as session:
        # NO guardamos la señal base, por lo que su ID no existirá en 'canonical_signals'
        # Al hacer flush() en la persistencia de Knowledge, Postgres lanzará IntegrityError (FK violation).
        with pytest.raises(IntegrityError):
            await run_cognitive_flow(base_signal, "Contexto", engine, None, session)

        # Como falló, hacemos rollback manual para limpiar la transacción fallida y poder seguir consultando
        await session.rollback()
        result = await session.execute(
            select(KnowledgeTransactionModel).where(
                KnowledgeTransactionModel.mission_id == base_signal.mission_id
            )
        )
        assert result.scalar_one_or_none() is None

@pytest.mark.integration
@pytest.mark.asyncio
async def test_cognitive_flow_accumulation_and_context(base_signal: CanonicalSignal):
    """
    Escenario D: Prueba la acumulación de contexto y asilamiento
    - Ejecuta Señal A -> produce Insight A.
    - Ejecuta Señal B (misma mission) -> el retriever provee Insight A al motor.
    """
    fake_response = json.dumps({
        "relevant": True,
        "evidence": "Evidencia nueva",
        "insight": "Insight acumulado",
        "confidence": 0.99,
        "reason": "Test acumulacion"
    })
    
    # Custom FakeLLM que guarda el prompt recibido
    class PromptCaptureLLM(FakeLLMProvider):
        def __init__(self, res):
            super().__init__(res)
            self.last_prompt = ""
        async def complete(self, prompt: str) -> str:
            self.last_prompt = prompt
            return await super().complete(prompt)
            
    llm = PromptCaptureLLM(fake_response)
    engine = CognitiveEngine(llm)
    
    from runtime.engines.cognitive.retriever import KnowledgeContextRetriever
    signal_a = CanonicalSignal(
        id=uuid4(),
        mission_id=base_signal.mission_id,
        sensor="test_sensor",
        source="test",
        content="Contenido de señal A",
        captured_at=datetime.now(UTC),
        source_event_id=uuid4()
    )
    
    signal_b = CanonicalSignal(
        id=uuid4(),
        mission_id=base_signal.mission_id,
        sensor="test_sensor",
        source="test",
        content="Contenido de señal B",
        captured_at=datetime.now(UTC),
        source_event_id=uuid4()
    )

    async with async_session() as session:
        # Guardar señales base
        sig_repo = CanonicalSignalRepository(session)
        await sig_repo.save(signal_a)
        await sig_repo.save(signal_b)
        await session.commit()
        
        # 1. Ejecutar Señal A
        retriever = KnowledgeContextRetriever(KnowledgeCoreRepository(session))
        view_a = await retriever.retrieve(signal_a.mission_id)
        tx_a = await run_cognitive_flow(signal_a, "Contexto test", engine, view_a, session)
        await session.commit()
        assert tx_a is not None
        
        prompt_a = llm.last_prompt
        assert "Sin conocimiento previo." in prompt_a
        
        # 2. Ejecutar Señal B (misma misión)
        view_b = await retriever.retrieve(signal_b.mission_id)
        tx_b = await run_cognitive_flow(signal_b, "Contexto test", engine, view_b, session)
        await session.commit()
        assert tx_b is not None
        
        prompt_b = llm.last_prompt
        assert "Sin conocimiento previo." not in prompt_b
        assert tx_a.insight.content in prompt_b  # El insight A fue inyectado
        
        # Limpieza
        await session.execute(delete(KnowledgeTransactionModel).where(KnowledgeTransactionModel.mission_id == base_signal.mission_id))
        await session.execute(delete(InsightModel).where(InsightModel.mission_id == base_signal.mission_id))
        await session.execute(delete(EvidenceModel).where(EvidenceModel.mission_id == base_signal.mission_id))
        await session.commit()
