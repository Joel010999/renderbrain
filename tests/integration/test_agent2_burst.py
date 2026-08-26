"""
tests/integration/test_agent2_burst.py

Burst Test offline para validar la resiliencia y el pipeline completo del Agente 2.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from runtime.contracts.event_envelope import EventEnvelope
from runtime.engines.cognitive.engine import CognitiveEngine
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.models.knowledge import (
    EvidenceModel,
    InsightModel,
    OpportunityModel,
    PatternModel,
)
from runtime.infrastructure.database.models.mission import ProcessedSignalModel
from runtime.infrastructure.database.session import async_session
from runtime.infrastructure.llm.errors import LLMProviderError
from runtime.workers.signal_worker import SignalWorker
from tests.fakes.fake_llm_provider import FakeLLMProvider


class BurstFakeLLMProvider(FakeLLMProvider):
    """
    Fake provider dinámico que responde según el prompt y el step para simular
    diversas condiciones en la ráfaga de señales.
    """
    def __init__(self):
        super().__init__("")
        self.step = 0

    async def complete(self, prompt: str) -> str:
        self.call_count += 1
        
        # 1. Relevance Detection (CognitiveFlow)
        if "Eres RenderBrain, el módulo cognitivo de inteligencia." in prompt or "filtro de relevancia" in prompt.lower() or "evalúa la relevancia" in prompt.lower() or "relevant" in prompt.lower():
            if self.step == 5 or self.step == 6:
                # Irrelevant signals
                return json.dumps({
                    "relevant": False,
                    "confidence": 0.9,
                    "evidence": None,
                    "insight": None,
                    "reason": "Irrelevant"
                })
            else:
                # Relevant signals
                return json.dumps({
                    "relevant": True,
                    "confidence": 0.9,
                    "evidence": "Evidence content",
                    "insight": "Insight content",
                    "reason": "Very relevant"
                })

        # 2. Pattern Detection
        if "Tu objetivo es identificar patrones o recurrencias" in prompt:
            # Pattern semantic failure
            if self.step == 10:
                # Indexes [99] are out of range for sure
                return json.dumps({
                    "pattern_found": True,
                    "content": "Invalid pattern",
                    "confidence": 0.9,
                    "supporting_insight_indexes": [99, 100],
                    "reason": "Bad indexes"
                })
            # Infra failure (Provider timeout)
            if self.step == 15:
                raise LLMProviderError("Simulated provider timeout")
                
            # Valid pattern (needs 2+ insights)
            # Assuming there are enough insights by step 7+
            if self.step >= 7:
                return json.dumps({
                    "pattern_found": True,
                    "content": f"Valid Pattern at step {self.step}",
                    "confidence": 0.9,
                    "supporting_insight_indexes": [0, 1],
                    "reason": "Valid"
                })
            else:
                return json.dumps({
                    "pattern_found": False
                })

        # 3. Opportunity Detection
        if "Tu objetivo es identificar una Oportunidad concreta" in prompt:
            # Opportunity semantic failure
            if self.step == 12:
                return json.dumps({
                    "opportunity_found": True,
                    "title": "Invalid Opportunity",
                    "description": "Invalid indexes",
                    "confidence": 0.9,
                    "supporting_pattern_indexes": [99],
                    "reason": "Bad index"
                })
                
            if self.step >= 8:
                return json.dumps({
                    "opportunity_found": True,
                    "title": f"Valid Opportunity at step {self.step}",
                    "description": "A very actionable strategic opportunity",
                    "confidence": 0.9,
                    "supporting_pattern_indexes": [0],
                    "reason": "Because pattern exists"
                })
            else:
                return json.dumps({
                    "opportunity_found": False
                })

        return "{}"


class DummyRedisConsumerGroup:
    def __init__(self):
        self.acked = []

    async def ack(self, entry_id: str) -> None:
        self.acked.append(entry_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_agent2_burst_resilience():
    """
    Simula una ráfaga de 20 señales para una misma misión, pasando por el pipeline del Agente 2.
    Verifica que:
    - Duplicados son ignorados (hit).
    - Irrelevantes completan limpio (sin insights, commit, XACK).
    - Fallos semánticos (InvalidPatternOutputError, InvalidOpportunitySupportError) se degradan de forma controlada sin perder la inteligencia válida previa.
    - Fallos de infraestructura no hacen XACK.
    - Se persisten correctamente CanonicalSignal, ProcessedSignal, Insights, Patterns y Opportunities.
    """
    mission_id = uuid4()
    mission_context = "Burst test mission"

    llm = BurstFakeLLMProvider()
    cognitive_engine = CognitiveEngine(llm_provider=llm)
    consumer_group = DummyRedisConsumerGroup()
    
    # Session factory for the worker
    session_maker = async_sessionmaker(async_session.kw["bind"], expire_on_commit=False)
    
    worker = SignalWorker(
        consumer_group=consumer_group,  # type: ignore
        session_factory=session_maker,
        cognitive_engine=cognitive_engine,
        llm_provider=llm,
        mission_context=mission_context,
    )

    total_signals = 20
    processed_count = 0
    infra_errors = 0

    for i in range(total_signals):
        llm.step = i
        
        # Step 2 and 3 will be duplicates (same content)
        content = f"Signal content {i}"
        if i == 3:
            content = "Signal content 2"  # Duplicate of step 2
            
        payload = {
            "mission_id": str(mission_id),
            "source": "instagram",
            "content": content,
            "metadata": {"native_id": f"post_{content}"},
            "captured_at": datetime.now(UTC).isoformat()
        }
        
        envelope = EventEnvelope(
            event_id=uuid4(),
            event_type="raw_signal_detected",
            payload=payload,
        )

        entry_id = f"entry-{i}"
        
        try:
            await worker.process_one(entry_id=entry_id, envelope=envelope)
            processed_count += 1
        except LLMProviderError:
            infra_errors += 1
            # Se espera que el mensaje 15 (step=15) falle con LLMProviderError
        except Exception as e:
            pytest.fail(f"Unexpected exception at step {i}: {e}")

    # --- Validations ---
    
    # 20 signals attempted, 1 infra error, so 19 should have completed without raising
    assert processed_count == 19
    assert infra_errors == 1
    
    # XACKs should be 19 (all except the infra error)
    assert len(consumer_group.acked) == 19
    assert "entry-15" not in consumer_group.acked

    async with async_session() as session:
        # Canonical signals: 19 processed, but step 3 was duplicate, so 18 unique canonical signals saved
        # Wait, if step 3 is duplicate, it is processed as _DUPLICATE, XACKed, but no CanonicalSignal is flushed
        canonical_count = await session.scalar(
            select(sa.func.count()).select_from(CanonicalSignalModel).where(CanonicalSignalModel.mission_id == mission_id)
        )
        assert canonical_count == 18
        
        # Processed signals: 18 unique fingerprints saved
        processed_count_db = await session.scalar(
            select(sa.func.count()).select_from(ProcessedSignalModel).where(ProcessedSignalModel.mission_id == mission_id)
        )
        assert processed_count_db == 18

        # Insights: 
        # Steps 5 and 6 were irrelevant -> no insight
        # Step 15 was infra error -> rollback -> no insight
        # Total unique successful signals = 18
        # Irrelevant = 2 (step 5, 6)
        # So insights should be 18 - 2 = 16
        insight_count = await session.scalar(
            select(sa.func.count()).select_from(InsightModel).where(InsightModel.mission_id == mission_id)
        )
        assert insight_count == 16
        
        # Patterns:
        # Started generating at step 7. Step 10 was semantic failure. Step 15 infra failure.
        # So we should have patterns for valid steps >= 7, minus step 10, minus step 15, minus duplicates/irrelevants if they didn't trigger
        # But we just need to ensure at least some patterns were saved, and trace exists.
        pattern_count = await session.scalar(
            select(sa.func.count()).select_from(PatternModel).where(PatternModel.mission_id == mission_id)
        )
        assert pattern_count > 0
        
        # Opportunities:
        # Started at step 8. Step 12 was semantic failure.
        # Should have some opportunities saved
        opp_count = await session.scalar(
            select(sa.func.count()).select_from(OpportunityModel).where(OpportunityModel.mission_id == mission_id)
        )
        assert opp_count > 0
        
        # Verify Traceability
        opp_model = (await session.execute(
            select(OpportunityModel).where(OpportunityModel.mission_id == mission_id).limit(1)
        )).scalar_one()
        
        # Verify priority is present
        assert opp_model.priority in ["low", "medium", "high"]
        assert opp_model.title is not None
        assert opp_model.description is not None
        
        # Tracing Opportunity -> Pattern
        # This requires querying the opportunity_patterns association table, or using the ORM relationship
        # The ORM relationships might not be loaded if we don't query with selectinload, but we can verify the DB.
        
        from sqlalchemy.orm import selectinload
        opp_with_patterns = (await session.execute(
            select(OpportunityModel)
            .options(selectinload(OpportunityModel.patterns))
            .where(OpportunityModel.id == opp_model.id)
        )).scalar_one()
        
        assert len(opp_with_patterns.patterns) > 0
        
        # Tracing Pattern -> Insight
        pattern_model = (await session.execute(
            select(PatternModel)
            .options(selectinload(PatternModel.insights))
            .where(PatternModel.id == opp_with_patterns.patterns[0].id)
        )).scalar_one()
        
        assert len(pattern_model.insights) >= 2
        
        # Tracing Insight -> Evidence -> CanonicalSignal
        insight_model = (await session.execute(
            select(InsightModel)
            .options(selectinload(InsightModel.evidence))
            .where(InsightModel.id == pattern_model.insights[0].id)
        )).scalar_one()
        
        evidence_model = insight_model.evidence
        assert evidence_model is not None
        
        canonical_model = (await session.execute(
            select(CanonicalSignalModel).where(CanonicalSignalModel.id == evidence_model.canonical_signal_id)
        )).scalar_one()
        
        assert canonical_model.mission_id == mission_id
        
        # Verify step 10 (Pattern semantic failure) didn't kill the signal's insight
        # We know step 10 executed. If it rolled back completely, we'd have 1 less insight.
        # With degraded pattern, the insight from step 10 should still be there.
        # Processed count = 18 and insight_count = 16 (excluding step 5,6) proves it.

    print("Burst test successfully validated resilience, determinism, and full traceability!")
