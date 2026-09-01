"""
tests/unit/test_content_strategist.py

Pruebas unitarias para el Content Strategist (Agent 3).
Cero llamadas a OpenAI (usa FakeLLMProvider).
"""

import json
from uuid import uuid4

import pytest

from runtime.contracts.content_brief import ContentAngle, ContentFormat, ContentObjective
from runtime.contracts.knowledge import Opportunity
from runtime.engines.content.strategist import ContentStrategist, InvalidContentBriefOutputError
from tests.fakes.fake_llm_provider import FakeLLMProvider


@pytest.fixture
def opportunity() -> Opportunity:
    return Opportunity(
        mission_id=uuid4(),
        title="Oportunidad de prueba",
        description="Descripción de la oportunidad",
        priority="high",
        confidence=0.9,
    )


def build_valid_json_response() -> str:
    return json.dumps({
        "content_format": "reel",
        "objective": "education",
        "target_audience": "Emprendedores",
        "angle": "pain",
        "core_message": "El control de stock manual mata tu negocio.",
        "hook": "¿Sigues usando Excel para tu inventario? Estás perdiendo dinero.",
        "sections": [
            {"order": 1, "title": "Intro", "content": "Slide 1 text"},
            {"order": 2, "title": None, "content": "Slide 2 text"}
        ],
        "cta": "Comenta INFO",
        "visual_direction": "Video dinámico, cortes rápidos.",
        "source_reasoning": "Resuelve la Opportunity X."
    })


@pytest.mark.asyncio
async def test_high_opportunity_generates_brief(opportunity):
    """Test 1: HIGH opportunity → ContentBrief generated (valid format reel)."""
    opportunity.priority = "high"
    llm = FakeLLMProvider(build_valid_json_response())
    strategist = ContentStrategist(llm)

    brief = await strategist.generate(opportunity, "Contexto de misión")

    assert brief.opportunity_id == opportunity.id
    assert brief.mission_id == opportunity.mission_id
    assert brief.content_format == ContentFormat.reel
    assert brief.objective == ContentObjective.education
    assert brief.angle == ContentAngle.pain
    assert brief.hook == "¿Sigues usando Excel para tu inventario? Estás perdiendo dinero."
    assert brief.cta == "Comenta INFO"
    assert brief.visual_direction == "Video dinámico, cortes rápidos."
    assert brief.source_reasoning == "Resuelve la Opportunity X."
    assert len(brief.sections) == 2
    assert brief.sections[0].order == 1
    assert brief.sections[0].content == "Slide 1 text"
    assert brief.sections[1].order == 2


@pytest.mark.asyncio
async def test_medium_opportunity_generates_brief(opportunity):
    """Test 2: MEDIUM opportunity → ContentBrief generated."""
    opportunity.priority = "medium"
    llm = FakeLLMProvider(build_valid_json_response())
    strategist = ContentStrategist(llm)

    brief = await strategist.generate(opportunity, "Contexto de misión")
    assert brief.opportunity_id == opportunity.id


@pytest.mark.asyncio
async def test_format_carousel(opportunity):
    """Test 4: Output is carousel format."""
    data = json.loads(build_valid_json_response())
    data["content_format"] = "carousel"
    llm = FakeLLMProvider(json.dumps(data))
    strategist = ContentStrategist(llm)

    brief = await strategist.generate(opportunity, "Contexto de misión")
    assert brief.content_format == ContentFormat.carousel


@pytest.mark.asyncio
async def test_format_static_post(opportunity):
    """Test 5: Output is static_post format."""
    data = json.loads(build_valid_json_response())
    data["content_format"] = "static_post"
    llm = FakeLLMProvider(json.dumps(data))
    strategist = ContentStrategist(llm)

    brief = await strategist.generate(opportunity, "Contexto de misión")
    assert brief.content_format == ContentFormat.static_post


@pytest.mark.asyncio
async def test_broken_json_raises_semantic_error(opportunity):
    """Test 12: Broken JSON → InvalidContentBriefOutputError."""
    llm = FakeLLMProvider("esto no es un { json")
    strategist = ContentStrategist(llm)

    with pytest.raises(InvalidContentBriefOutputError) as exc:
        await strategist.generate(opportunity, "Context")
    assert "JSON inválido" in str(exc.value)


@pytest.mark.asyncio
async def test_missing_required_field_raises_semantic_error(opportunity):
    """Test 13: Missing required field → InvalidContentBriefOutputError."""
    data = json.loads(build_valid_json_response())
    del data["hook"]
    llm = FakeLLMProvider(json.dumps(data))
    strategist = ContentStrategist(llm)

    with pytest.raises(InvalidContentBriefOutputError) as exc:
        await strategist.generate(opportunity, "Context")
    assert "no cumple el schema" in str(exc.value)


@pytest.mark.asyncio
async def test_empty_hook_raises_semantic_error(opportunity):
    """Test 6: hook field non-empty."""
    data = json.loads(build_valid_json_response())
    data["hook"] = "   "
    llm = FakeLLMProvider(json.dumps(data))
    strategist = ContentStrategist(llm)

    with pytest.raises(InvalidContentBriefOutputError) as exc:
        await strategist.generate(opportunity, "Context")
    assert "obligatorio y no puede estar vacío" in str(exc.value)


@pytest.mark.asyncio
async def test_empty_sections_raises_semantic_error(opportunity):
    """Test 8: sections has at least 1 item."""
    data = json.loads(build_valid_json_response())
    data["sections"] = []
    llm = FakeLLMProvider(json.dumps(data))
    strategist = ContentStrategist(llm)

    with pytest.raises(InvalidContentBriefOutputError) as exc:
        await strategist.generate(opportunity, "Context")
    assert "no proveyó ninguna sección" in str(exc.value)


class ExplodingLLMProvider:
    async def complete(self, prompt: str) -> str:
        raise RuntimeError("Network timeout")


@pytest.mark.asyncio
async def test_provider_timeout_propagates(opportunity):
    """Test 14: Provider timeout (RuntimeError) ≠ InvalidContentBriefOutputError."""
    llm = ExplodingLLMProvider()
    strategist = ContentStrategist(llm)

    with pytest.raises(RuntimeError) as exc:
        await strategist.generate(opportunity, "Context")
    assert "Network timeout" in str(exc.value)
