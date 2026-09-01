import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from runtime.contracts.content_brief import ContentBrief
from runtime.contracts.knowledge import Opportunity
from runtime.engines.content.strategist import ContentStrategist
from runtime.infrastructure.llm.interfaces import LLMProvider
from runtime.orchestration.content_strategy_flow import run_content_strategy_flow


class DelayedFakeLLMProvider(LLMProvider):
    """Fake provider that sleeps to simulate a delay."""
    def __init__(self, delay_seconds: float, response: str = "{}"):
        self.delay_seconds = delay_seconds
        self.response = response

    async def complete(self, prompt: str) -> str:
        await asyncio.sleep(self.delay_seconds)
        return self.response


@pytest.mark.asyncio
async def test_content_strategist_timeout():
    """Test B: Provider takes too long -> Raises RuntimeError due to asyncio.wait_for timeout."""
    opp = Opportunity(
        mission_id=uuid4(),
        title="Oportunidad",
        description="Desc",
        priority="high",
    )
    # The timeout is hardcoded to 60.0s. To test it without waiting 60s,
    # we patch asyncio.wait_for to use a tiny timeout.
    
    provider = DelayedFakeLLMProvider(delay_seconds=0.1)
    strategist = ContentStrategist(provider)

    with patch("asyncio.wait_for") as mock_wait_for:
        # Simulate wait_for raising TimeoutError
        mock_wait_for.side_effect = asyncio.TimeoutError()

        with pytest.raises(RuntimeError) as exc:
            await strategist.generate(opp, "Context")
        
        assert "tiempo límite" in str(exc.value)


@pytest.mark.asyncio
@patch("runtime.orchestration.content_strategy_flow.ContentBriefRepository")
async def test_run_content_strategy_flow_catches_timeout(mock_repo_class):
    """Test B (Flow level): Flow catches the timeout error and returns None, protecting the loop."""
    opp = Opportunity(
        mission_id=uuid4(),
        title="Oportunidad",
        description="Desc",
        priority="high",
    )
    provider = DelayedFakeLLMProvider(delay_seconds=0.1)

    mock_session_factory = MagicMock()
    mock_session = AsyncMock()
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = mock_session
    mock_cm.__aexit__.return_value = None
    mock_session_factory.return_value = mock_cm
    
    mock_mission = MagicMock()
    mock_mission.observation_scope = "reference"
    mock_session.get.return_value = mock_mission

    with patch("asyncio.wait_for") as mock_wait_for:
        mock_wait_for.side_effect = asyncio.TimeoutError()
        
        # Debe retornar None y NO lanzar excepción, permitiendo que el batch continúe
        brief = await run_content_strategy_flow(
            opportunity=opp,
            mission_context="Context",
            llm_provider=provider,
            session_factory=mock_session_factory,
        )

        assert brief is None
        mock_session.commit.assert_not_called()
