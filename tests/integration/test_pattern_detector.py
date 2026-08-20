import json
import pytest
from uuid import uuid4
from datetime import datetime, UTC

from runtime.contracts.knowledge import MissionIntelligenceView, InsightSummary
from runtime.engines.cognitive.pattern_detector import PatternDetector
from tests.infrastructure.llm.test_llm_adapters import FakeLLMProvider

@pytest.mark.asyncio
async def test_pattern_detector_below_threshold_returns_none():
    llm = FakeLLMProvider("{}")
    detector = PatternDetector(llm, min_insights_threshold=3)
    
    view = MissionIntelligenceView(
        mission_id=uuid4(),
        insights=[
            InsightSummary(id=uuid4(), content="Insight 1", created_at=datetime.now(UTC)),
            InsightSummary(id=uuid4(), content="Insight 2", created_at=datetime.now(UTC))
        ]
    )
    
    pattern, ids = await detector.detect(view.mission_id, "Context", view)
    assert pattern is None
    assert ids == []

@pytest.mark.asyncio
async def test_pattern_detector_pattern_not_found_returns_none():
    fake_response = json.dumps({"pattern_found": False})
    llm = FakeLLMProvider(fake_response)
    detector = PatternDetector(llm, min_insights_threshold=3)
    
    view = MissionIntelligenceView(
        mission_id=uuid4(),
        insights=[
            InsightSummary(id=uuid4(), content="Insight 1", created_at=datetime.now(UTC)),
            InsightSummary(id=uuid4(), content="Insight 2", created_at=datetime.now(UTC)),
            InsightSummary(id=uuid4(), content="Insight 3", created_at=datetime.now(UTC))
        ]
    )
    
    pattern, ids = await detector.detect(view.mission_id, "Context", view)
    assert pattern is None
    assert ids == []

@pytest.mark.asyncio
async def test_pattern_detector_valid_response_creates_pattern():
    fake_response = json.dumps({
        "pattern_found": True,
        "content": "A detected pattern",
        "confidence": 0.95,
        "supporting_insight_indexes": [0, 2],
        "reason": "Because it repeats"
    })
    llm = FakeLLMProvider(fake_response)
    detector = PatternDetector(llm, min_insights_threshold=3)
    
    id_1 = uuid4()
    id_2 = uuid4()
    id_3 = uuid4()
    mission_id = uuid4()
    
    view = MissionIntelligenceView(
        mission_id=mission_id,
        insights=[
            InsightSummary(id=id_1, content="Insight 1", created_at=datetime.now(UTC)),
            InsightSummary(id=id_2, content="Insight 2", created_at=datetime.now(UTC)),
            InsightSummary(id=id_3, content="Insight 3", created_at=datetime.now(UTC))
        ]
    )
    
    pattern, ids = await detector.detect(mission_id, "Context", view)
    
    assert pattern is not None
    assert pattern.content == "A detected pattern"
    assert pattern.confidence == 0.95
    assert pattern.support_count == 2
    assert pattern.mission_id == mission_id
    assert ids == [id_1, id_3]

@pytest.mark.asyncio
async def test_pattern_detector_invalid_indexes_raises_error():
    fake_response = json.dumps({
        "pattern_found": True,
        "content": "A detected pattern",
        "confidence": 0.95,
        "supporting_insight_indexes": [0, 5], # 5 is out of range
        "reason": "Because it repeats"
    })
    llm = FakeLLMProvider(fake_response)
    detector = PatternDetector(llm, min_insights_threshold=3)
    
    view = MissionIntelligenceView(
        mission_id=uuid4(),
        insights=[
            InsightSummary(id=uuid4(), content="Insight 1", created_at=datetime.now(UTC)),
            InsightSummary(id=uuid4(), content="Insight 2", created_at=datetime.now(UTC)),
            InsightSummary(id=uuid4(), content="Insight 3", created_at=datetime.now(UTC))
        ]
    )
    
    with pytest.raises(ValueError, match="Índice de soporte fuera de rango: 5"):
        await detector.detect(view.mission_id, "Context", view)

@pytest.mark.asyncio
async def test_pattern_detector_less_than_two_supports_raises_error():
    fake_response = json.dumps({
        "pattern_found": True,
        "content": "A detected pattern",
        "confidence": 0.95,
        "supporting_insight_indexes": [1], # Need at least 2
        "reason": "Because it repeats"
    })
    llm = FakeLLMProvider(fake_response)
    detector = PatternDetector(llm, min_insights_threshold=3)
    
    view = MissionIntelligenceView(
        mission_id=uuid4(),
        insights=[
            InsightSummary(id=uuid4(), content="Insight 1", created_at=datetime.now(UTC)),
            InsightSummary(id=uuid4(), content="Insight 2", created_at=datetime.now(UTC)),
            InsightSummary(id=uuid4(), content="Insight 3", created_at=datetime.now(UTC))
        ]
    )
    
    with pytest.raises(ValueError, match="Se requieren al menos 2 índices de soporte"):
        await detector.detect(view.mission_id, "Context", view)
