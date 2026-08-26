import pytest
from uuid import uuid4
from sqlalchemy import delete
from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.knowledge import Evidence, Insight, KnowledgeTransaction
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.knowledge import EvidenceModel, InsightModel, KnowledgeTransactionModel
from runtime.infrastructure.database.repositories.knowledge import KnowledgeCoreRepository
from runtime.engines.cognitive.retriever import KnowledgeContextRetriever

@pytest.fixture
async def setup_db():
    async with async_session() as session:
        yield session

async def _create_dummy_transaction(session, mission_id, content, confidence) -> KnowledgeTransaction:
    # Generar un signal_id unico por transaccion para evitar colisiones
    signal_id = uuid4()
    # Asegurar que el signal exista para la FK
    from runtime.infrastructure.database.repositories.canonical_signal import CanonicalSignalRepository
    from datetime import datetime, UTC
    sig = CanonicalSignal(id=signal_id, mission_id=mission_id, sensor="test_sensor", source="test", content="x", captured_at=datetime.now(UTC), source_event_id=uuid4())
    sig_repo = CanonicalSignalRepository(session)
    await sig_repo.save(sig)
    await session.flush()

    ev = Evidence(mission_id=mission_id, canonical_signal_id=signal_id, content=f"ev {content}", confidence=confidence)
    ins = Insight(mission_id=mission_id, evidence_id=ev.id, content=f"ins {content}", confidence=confidence)
    tx = KnowledgeTransaction(mission_id=mission_id, action="CREATE", evidence=ev, insight=ins, producer="test")
    repo = KnowledgeCoreRepository(session)
    await repo.commit(tx)
    return tx


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retriever_empty_context():
    mission_id = uuid4()
    async with async_session() as session:
        repo = KnowledgeCoreRepository(session)
        retriever = KnowledgeContextRetriever(repo)
        
        context = await retriever.retrieve(mission_id)
        assert context.mission_id == mission_id
        assert len(context.insights) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retriever_returns_recent_insights():
    mission_id = uuid4()
    async with async_session() as session:
        await _create_dummy_transaction(session, mission_id, "first", 0.9)
        await _create_dummy_transaction(session, mission_id, "second", 0.8)
        await session.commit()
        
        repo = KnowledgeCoreRepository(session)
        retriever = KnowledgeContextRetriever(repo)
        context = await retriever.retrieve(mission_id, insight_limit=10)
        
        assert len(context.insights) == 2
        # El más reciente debe estar primero (desc)
        assert context.insights[0].content == "ins second"
        assert context.insights[1].content == "ins first"
        
        # Limpiar
        await session.execute(delete(KnowledgeTransactionModel).where(KnowledgeTransactionModel.mission_id == mission_id))
        await session.execute(delete(InsightModel).where(InsightModel.mission_id == mission_id))
        await session.execute(delete(EvidenceModel).where(EvidenceModel.mission_id == mission_id))
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retriever_isolation_between_missions():
    mission_a = uuid4()
    mission_b = uuid4()
    async with async_session() as session:
        await _create_dummy_transaction(session, mission_a, "A", 0.9)
        await _create_dummy_transaction(session, mission_b, "B", 0.8)
        await session.commit()
        
        repo = KnowledgeCoreRepository(session)
        retriever = KnowledgeContextRetriever(repo)
        
        context_a = await retriever.retrieve(mission_a)
        assert len(context_a.insights) == 1
        assert context_a.insights[0].content == "ins A"
        
        context_b = await retriever.retrieve(mission_b)
        assert len(context_b.insights) == 1
        assert context_b.insights[0].content == "ins B"

        # Limpiar
        await session.execute(delete(KnowledgeTransactionModel).where(KnowledgeTransactionModel.mission_id.in_([mission_a, mission_b])))
        await session.execute(delete(InsightModel).where(InsightModel.mission_id.in_([mission_a, mission_b])))
        await session.execute(delete(EvidenceModel).where(EvidenceModel.mission_id.in_([mission_a, mission_b])))
        await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_retriever_limits_and_order():
    mission_id = uuid4()
    async with async_session() as session:
        # Create 25 insights, 15 patterns, 6 opportunities
        # Actually just mocking them via DB inserts
        from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
        from runtime.infrastructure.database.models.knowledge import EvidenceModel, InsightModel, PatternModel, OpportunityModel
        from datetime import datetime, timedelta, UTC
        
        base_time = datetime.now(UTC) - timedelta(days=1)
        
        c_id = uuid4()
        session.add(CanonicalSignalModel(
            id=c_id, mission_id=mission_id, source='dummy', sensor='dummy',
            source_event_id=uuid4(), metrics={'k': 'v'}, content='Dummy signal',
            captured_at=base_time, normalized_at=base_time
        ))
        
        e_id = uuid4()
        session.add(EvidenceModel(
            id=e_id, mission_id=mission_id, canonical_signal_id=c_id,
            content='Dummy evidence', created_at=base_time
        ))
        
        for i in range(25):
            session.add(InsightModel(
                id=uuid4(), mission_id=mission_id, evidence_id=e_id,
                content=f'Insight {i}', created_at=base_time + timedelta(seconds=i)
            ))
            
        for i in range(15):
            session.add(PatternModel(
                id=uuid4(), mission_id=mission_id, content=f'Pattern {i}',
                support_count=2, created_at=base_time + timedelta(seconds=i)
            ))
            
        for i in range(6):
            session.add(OpportunityModel(
                id=uuid4(), mission_id=mission_id, title=f'Opp {i}', description=f'Opportunity {i}', priority='medium',
                created_at=base_time + timedelta(seconds=i)
            ))
            
        await session.commit()
        
        repo = KnowledgeCoreRepository(session)
        retriever = KnowledgeContextRetriever(repo)
        
        # Test defaults
        context = await retriever.retrieve(mission_id)
        
        assert len(context.insights) == 20
        assert len(context.patterns) == 10
        assert len(context.opportunities) == 5
        
        # Order should be DESC, so Insight 24 is first, Insight 5 is last (0-24 -> 24 to 5 is 20 items)
        assert context.insights[0].content == 'Insight 24'
        assert context.insights[-1].content == 'Insight 5'
        
        assert context.patterns[0].content == 'Pattern 14'
        assert context.patterns[-1].content == 'Pattern 5'
        
        assert context.opportunities[0].description == 'Opportunity 5'
        assert context.opportunities[-1].description == 'Opportunity 1'
        
        # Test explicit limits
        context2 = await retriever.retrieve(mission_id, insight_limit=5, pattern_limit=3, opportunity_limit=2)
        assert len(context2.insights) == 5
        assert len(context2.patterns) == 3
        assert len(context2.opportunities) == 2
        
        # Clean up
        from sqlalchemy import delete
        await session.execute(delete(OpportunityModel).where(OpportunityModel.mission_id == mission_id))
        await session.execute(delete(PatternModel).where(PatternModel.mission_id == mission_id))
        await session.execute(delete(InsightModel).where(InsightModel.mission_id == mission_id))
        await session.execute(delete(EvidenceModel).where(EvidenceModel.mission_id == mission_id))
        await session.execute(delete(CanonicalSignalModel).where(CanonicalSignalModel.mission_id == mission_id))
        await session.commit()

