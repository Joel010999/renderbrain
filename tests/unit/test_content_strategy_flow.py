"""
tests/unit/test_content_strategy_flow.py
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from runtime.contracts.content_brief import ContentBrief
from runtime.contracts.knowledge import Opportunity
from runtime.engines.content.strategist import InvalidContentBriefOutputError
from runtime.orchestration.content_strategy_flow import run_content_strategy_flow
from tests.fakes.fake_llm_provider import FakeLLMProvider


@pytest.fixture
def opportunity() -> Opportunity:
    return Opportunity(
        mission_id=uuid4(),
        title="Oportunidad",
        description="Desc",
        priority="high",
    )


def build_valid_json() -> str:
    return json.dumps({
        "content_format": "reel",
        "objective": "education",
        "target_audience": "Emprendedores",
        "angle": "pain",
        "core_message": "El control de stock manual mata tu negocio.",
        "hook": "¿Sigues usando Excel para tu inventario? Estás perdiendo dinero.",
        "sections": [
            {"order": 1, "title": "Intro", "content": "Slide 1 text"}
        ],
        "cta": "Comenta INFO",
        "visual_direction": "Video dinámico.",
        "source_reasoning": "Resuelve la Opportunity X."
    })


@pytest.mark.asyncio
@patch("runtime.orchestration.content_strategy_flow.ContentBriefRepository")
async def test_flow_success(mock_repo_class, opportunity):
    """Test E2E Fake: Valid flow -> generates brief, saves it."""
    llm = FakeLLMProvider(build_valid_json())
    mock_session_factory = MagicMock()
    mock_session = AsyncMock()
    
    # Setup async context manager magic methods
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_session
    mock_cm.__aexit__.return_value = None
    mock_session_factory.return_value = mock_cm

    mock_repo = AsyncMock()
    mock_repo_class.return_value = mock_repo
    mock_repo.save_if_not_exists.return_value = True

    brief = await run_content_strategy_flow(
        opportunity=opportunity,
        mission_context="Context",
        llm_provider=llm,
        session_factory=mock_session_factory,
    )

    assert brief is not None
    assert isinstance(brief, ContentBrief)
    mock_repo.save_if_not_exists.assert_called_once_with(brief)
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
@patch("runtime.orchestration.content_strategy_flow.ContentBriefRepository")
async def test_flow_semantic_error_caught(mock_repo_class, opportunity):
    """Test 15: Agent 3 failure does NOT rollback Agent 2 Opportunity."""
    # LLM returns broken JSON -> InvalidContentBriefOutputError
    llm = FakeLLMProvider("bad json")
    mock_session_factory = MagicMock()
    mock_session = AsyncMock()
    
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_session
    mock_cm.__aexit__.return_value = None
    mock_session_factory.return_value = mock_cm

    brief = await run_content_strategy_flow(
        opportunity=opportunity,
        mission_context="Context",
        llm_provider=llm,
        session_factory=mock_session_factory,
    )

    # Returns None, doesn't raise
    assert brief is None
    # Ensure commit was never called since it failed semantically
    mock_session.commit.assert_not_called()


@pytest.mark.asyncio
@patch("runtime.orchestration.content_strategy_flow.ContentBriefRepository")
async def test_flow_idempotency_already_exists(mock_repo_class, opportunity):
    """Test 16: Same opportunity_id -> no second brief (idempotency)."""
    llm = FakeLLMProvider(build_valid_json())
    mock_session_factory = MagicMock()
    mock_session = AsyncMock()
    
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_session
    mock_cm.__aexit__.return_value = None
    mock_session_factory.return_value = mock_cm

    mock_repo = AsyncMock()
    mock_repo_class.return_value = mock_repo
    # Simulate conflict DO NOTHING
    mock_repo.save_if_not_exists.return_value = False

    brief = await run_content_strategy_flow(
        opportunity=opportunity,
        mission_context="Context",
        llm_provider=llm,
        session_factory=mock_session_factory,
    )

    assert brief is not None
    mock_session.commit.assert_called_once()
