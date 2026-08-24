"""
tests/engines/cognitive/test_opportunity_detector.py

Tests unitarios offline para OpportunityDetector (Hotfix v1.0.2).

Garantías:
  - CERO llamadas a OpenAI/Apify. Usa FakeLLMProvider exclusivamente.
  - CERO acceso a PostgreSQL/Redis.
  - No marcados como @pytest.mark.integration ni @pytest.mark.external.

Casos cubiertos:
  1. Regresión exacta: índice [2] con 1 pattern → InvalidOpportunitySupportError.
  2. Índice válido [0] con 1 pattern → Opportunity retornada correctamente.
  3. LLM infra error → excepción fatal propagada (no InvalidOpportunitySupportError).
  4. 0 patrones en vista → (None, []) sin llamar al LLM.
  5. opportunity_found=False → (None, []) sin error.
  6. Prompt test: el prompt compilado contiene índices [0] y labels correctos.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from runtime.contracts.knowledge import MissionIntelligenceView, PatternSummary
from runtime.engines.cognitive.opportunity_detector import (
    InvalidOpportunitySupportError,
    OpportunityDetector,
)
from runtime.infrastructure.llm.errors import LLMProviderError
from tests.fakes.fake_llm_provider import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pattern_summary(content: str = "A pattern") -> PatternSummary:
    return PatternSummary(
        id=uuid4(),
        content=content,
        confidence=0.9,
        support_count=2,
        created_at=datetime.now(UTC),
    )


def _make_view(num_patterns: int = 1) -> MissionIntelligenceView:
    return MissionIntelligenceView(
        mission_id=uuid4(),
        insights=[],
        patterns=[_make_pattern_summary(f"Pattern {i}") for i in range(num_patterns)],
        opportunities=[],
    )


def _opp_response(indexes: list[int]) -> str:
    return json.dumps({
        "opportunity_found": True,
        "content": "Strategic action derived from patterns",
        "confidence": 0.88,
        "supporting_pattern_indexes": indexes,
        "reason": "Patterns support this opportunity",
    })


# ---------------------------------------------------------------------------
# Test 1 — Regresión exacta: índice [2] con 1 pattern → InvalidOpportunitySupportError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_index_out_of_range_raises_invalid_support_error():
    """
    TEST DE REGRESIÓN v1.0.2.

    Reproduce el bug de producción:
    1 pattern en la vista. LLM fake devuelve supporting_pattern_indexes: [2].
    Resultado esperado: InvalidOpportunitySupportError (NO ValueError genérico).

    Confirma que la excepción es cognitiva y controlada — puede ser capturada
    por el worker sin destruir la inteligencia válida ya generada.
    """
    view = _make_view(num_patterns=1)
    llm = FakeLLMProvider(_opp_response(indexes=[2]))  # índice 2 con solo 1 pattern

    detector = OpportunityDetector(llm_provider=llm)

    with pytest.raises(InvalidOpportunitySupportError) as exc_info:
        await detector.detect(
            mission_id=view.mission_id,
            mission_context="Mission context for regression test",
            intelligence_view=view,
        )

    # Verificar que el mensaje describe el problema claramente
    assert "2" in str(exc_info.value), "El mensaje debe mencionar el índice inválido"
    assert "1" in str(exc_info.value), "El mensaje debe mencionar el total de patterns"

    # Confirmar que es subclase de ValueError (retrocompatibilidad)
    assert isinstance(exc_info.value, ValueError)


# ---------------------------------------------------------------------------
# Test 2 — Índice válido [0] → Opportunity persiste con asociación correcta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_index_zero_returns_opportunity():
    """
    Índice [0] con 1 pattern disponible.
    Resultado esperado: Opportunity retornada, supporting_ids contiene el UUID del pattern[0].
    """
    view = _make_view(num_patterns=1)
    expected_pattern_id = view.patterns[0].id

    llm = FakeLLMProvider(_opp_response(indexes=[0]))
    detector = OpportunityDetector(llm_provider=llm)

    opportunity, supporting_ids = await detector.detect(
        mission_id=view.mission_id,
        mission_context="Mission context for valid index test",
        intelligence_view=view,
    )

    assert opportunity is not None, "Debe retornar Opportunity con índice [0] válido"
    assert opportunity.content == "Strategic action derived from patterns"
    assert opportunity.mission_id == view.mission_id
    assert len(supporting_ids) == 1
    assert supporting_ids[0] == expected_pattern_id, (
        f"supporting_ids debe contener el UUID del pattern[0].\n"
        f"  Esperado: {expected_pattern_id}\n"
        f"  Recibido: {supporting_ids[0]}"
    )

    # El LLM fue llamado exactamente una vez
    assert llm.call_count == 1


# ---------------------------------------------------------------------------
# Test 3 — Error de infraestructura → excepción fatal propagada sin catch controlado
# ---------------------------------------------------------------------------


class _TimeoutLLMProvider:
    """Simula un timeout del provider de LLM (error de infraestructura real)."""

    async def complete(self, prompt: str) -> str:  # noqa: ARG002
        raise LLMProviderError("Simulated provider timeout for infra error test")


@pytest.mark.asyncio
async def test_infra_error_propagates_as_fatal_exception():
    """
    Error de infraestructura en el LLM provider → excepción fatal propagada.
    NO debe ser capturado como InvalidOpportunitySupportError.
    El caller (SignalWorker) debe hacer rollback y NO XACK.
    """
    view = _make_view(num_patterns=1)
    detector = OpportunityDetector(llm_provider=_TimeoutLLMProvider())  # type: ignore[arg-type]

    with pytest.raises(LLMProviderError, match="Simulated provider timeout"):
        await detector.detect(
            mission_id=view.mission_id,
            mission_context="Mission context for infra error test",
            intelligence_view=view,
        )


# ---------------------------------------------------------------------------
# Test 4 — 0 patrones en la vista → (None, []) sin llamar al LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_patterns_returns_none_without_llm_call():
    """
    Vista con 0 patrones disponibles.
    Resultado esperado: (None, []) sin ninguna llamada al LLM.
    """
    view = _make_view(num_patterns=0)
    llm = FakeLLMProvider("this should never be called")
    detector = OpportunityDetector(llm_provider=llm)

    opportunity, supporting_ids = await detector.detect(
        mission_id=view.mission_id,
        mission_context="Mission context",
        intelligence_view=view,
    )

    assert opportunity is None
    assert supporting_ids == []
    assert llm.call_count == 0, "El LLM NO debe ser llamado si no hay patterns"


# ---------------------------------------------------------------------------
# Test 5 — opportunity_found=False → (None, []) sin error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_opportunity_not_found_returns_none():
    """
    LLM indica opportunity_found=False.
    Resultado esperado: (None, []) sin levantar excepción.
    """
    view = _make_view(num_patterns=2)
    response = json.dumps({
        "opportunity_found": False,
        "content": None,
        "confidence": None,
        "supporting_pattern_indexes": None,
        "reason": "No actionable opportunity identified",
    })
    llm = FakeLLMProvider(response)
    detector = OpportunityDetector(llm_provider=llm)

    opportunity, supporting_ids = await detector.detect(
        mission_id=view.mission_id,
        mission_context="Mission context",
        intelligence_view=view,
    )

    assert opportunity is None
    assert supporting_ids == []


# ---------------------------------------------------------------------------
# Test 6 — Prompt test: el prompt contiene índices zero-based correctos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_contains_zero_based_indexes():
    """
    Verifica que el prompt compilado usa índices [0], [1], ... (zero-based)
    y contiene la instrucción explícita sobre zero-based indexing.

    Este test previene regresiones donde el prompt muestre 1-based o ambiguo.
    """
    captured_prompts: list[str] = []

    class _CapturingLLMProvider:
        async def complete(self, prompt: str) -> str:
            captured_prompts.append(prompt)
            # Retornar opportunity_found=False para no necesitar validación de índices
            return json.dumps({
                "opportunity_found": False,
                "reason": "Captured prompt for inspection",
            })

    view = _make_view(num_patterns=3)
    # Personalizar contenido para hacer verificación clara
    view.patterns[0].content = "Alpha pattern"
    view.patterns[1].content = "Beta pattern"
    view.patterns[2].content = "Gamma pattern"

    detector = OpportunityDetector(llm_provider=_CapturingLLMProvider())  # type: ignore[arg-type]
    await detector.detect(
        mission_id=view.mission_id,
        mission_context="Prompt inspection context",
        intelligence_view=view,
    )

    assert len(captured_prompts) == 1, "Se debe haber generado exactamente 1 prompt"
    prompt = captured_prompts[0]

    # Verificar índices zero-based presentes
    assert "[0]" in prompt, "El prompt debe contener el índice [0]"
    assert "[1]" in prompt, "El prompt debe contener el índice [1]"
    assert "[2]" in prompt, "El prompt debe contener el índice [2]"

    # Verificar instrucción explícita zero-based
    assert "zero-based" in prompt, (
        "El prompt debe contener la instrucción explícita 'zero-based'"
    )
    assert "MUST use exactly the zero-based indexes" in prompt, (
        "El prompt debe contener la instrucción canónica de zero-based indexing"
    )

    # Verificar contenido de patterns mapeado correctamente
    assert "Alpha pattern" in prompt
    assert "Beta pattern" in prompt
    assert "Gamma pattern" in prompt


# ---------------------------------------------------------------------------
# Test 7 — Índice negativo → InvalidOpportunitySupportError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_index_raises_invalid_support_error():
    """
    Índice negativo (-1) → InvalidOpportunitySupportError.
    Cubre el branch `idx < 0` de la validación de índices.
    """
    view = _make_view(num_patterns=2)
    llm = FakeLLMProvider(_opp_response(indexes=[-1]))

    detector = OpportunityDetector(llm_provider=llm)

    with pytest.raises(InvalidOpportunitySupportError):
        await detector.detect(
            mission_id=view.mission_id,
            mission_context="Mission context for negative index test",
            intelligence_view=view,
        )


# ---------------------------------------------------------------------------
# Test 8 — Múltiples patrones, índices válidos → Opportunity con múltiple soporte
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_valid_indexes_return_correct_supporting_ids():
    """
    3 patrones en la vista. LLM devuelve indexes [0, 2].
    Resultado: Opportunity con 2 supporting_ids correctos (patterns[0] y patterns[2]).
    """
    view = _make_view(num_patterns=3)
    expected_ids = {view.patterns[0].id, view.patterns[2].id}

    llm = FakeLLMProvider(_opp_response(indexes=[0, 2]))
    detector = OpportunityDetector(llm_provider=llm)

    opportunity, supporting_ids = await detector.detect(
        mission_id=view.mission_id,
        mission_context="Mission context for multi-index test",
        intelligence_view=view,
    )

    assert opportunity is not None
    assert set(supporting_ids) == expected_ids, (
        f"Los supporting_ids deben ser {expected_ids}, recibido: {set(supporting_ids)}"
    )
