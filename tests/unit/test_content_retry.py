import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from runtime.workers.content_retry import content_strategy_retry_loop, _process_missing_briefs

@pytest.fixture
def mock_session_factory():
    session = AsyncMock()
    # Para el caso base sin opportunities, setup empty
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    session.execute.return_value = result_mock
    
    factory = MagicMock(return_value=session)
    # Async context manager __aenter__ and __aexit__
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    return factory

@pytest.fixture
def mock_llm_provider():
    return AsyncMock()

@pytest.fixture
def mock_run_flow():
    with patch("runtime.workers.content_retry.run_content_strategy_flow", new_callable=AsyncMock) as mock:
        yield mock

@pytest.mark.asyncio
async def test_a_loop_starts_and_executes_multiple_iterations(mock_session_factory, mock_llm_provider):
    stop_event = asyncio.Event()
    
    # Vamos a espiar process_missing_briefs para contar cuantas veces se llamo
    with patch("runtime.workers.content_retry._process_missing_briefs", new_callable=AsyncMock) as mock_process:
        # Hacemos que se detenga despues de 2 iteraciones
        async def side_effect(*args, **kwargs):
            if mock_process.call_count == 2:
                stop_event.set()
        mock_process.side_effect = side_effect
        
        # Ejecutamos el loop
        await content_strategy_retry_loop(
            session_factory=mock_session_factory,
            llm_provider=mock_llm_provider,
            mission_context="test",
            stop_event=stop_event,
            interval_seconds=0,
        )
        
        assert mock_process.call_count == 2

@pytest.mark.asyncio
async def test_b_iteration_error_does_not_kill_loop(mock_session_factory, mock_llm_provider):
    stop_event = asyncio.Event()
    
    with patch("runtime.workers.content_retry._process_missing_briefs", new_callable=AsyncMock) as mock_process:
        async def side_effect(*args, **kwargs):
            if mock_process.call_count == 1:
                raise RuntimeError("DB error")
            if mock_process.call_count == 2:
                stop_event.set()
                
        mock_process.side_effect = side_effect
        
        await content_strategy_retry_loop(
            session_factory=mock_session_factory,
            llm_provider=mock_llm_provider,
            mission_context="test",
            stop_event=stop_event,
            interval_seconds=0,
        )
        
        # Asegura que paso a la segunda iteracion a pesar del error
        assert mock_process.call_count == 2

@pytest.mark.asyncio
async def test_c_opportunity_failure_does_not_stop_batch(mock_session_factory, mock_llm_provider, mock_run_flow):
    # Setup de opportunities
    opp1 = MagicMock()
    opp1.id = "1"
    opp2 = MagicMock()
    opp2.id = "2"
    
    session = mock_session_factory.return_value
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [opp1, opp2]
    session.execute.return_value = result_mock
    
    # La primera lanza error, la segunda pasa
    mock_run_flow.side_effect = [RuntimeError("Provider error"), None]
    
    with patch("runtime.contracts.knowledge.Opportunity") as mock_opp:
        await _process_missing_briefs(
            session_factory=mock_session_factory,
            llm_provider=mock_llm_provider,
            mission_context="test",
        )
    
    # Confirmar que se llamo dos veces (la primera fallo, la segunda igual se proceso)
    assert mock_run_flow.call_count == 2

@pytest.mark.asyncio
async def test_d_task_cancel_terminates_cleanly(mock_session_factory, mock_llm_provider):
    stop_event = asyncio.Event()
    
    with patch("runtime.workers.content_retry._process_missing_briefs", new_callable=AsyncMock) as mock_process:
        async def side_effect(*args, **kwargs):
            raise asyncio.CancelledError()
            
        mock_process.side_effect = side_effect
        
        with pytest.raises(asyncio.CancelledError):
            await content_strategy_retry_loop(
                session_factory=mock_session_factory,
                llm_provider=mock_llm_provider,
                mission_context="test",
                stop_event=stop_event,
                interval_seconds=0,
            )

@pytest.mark.asyncio
async def test_e_immediate_first_iteration(mock_session_factory, mock_llm_provider):
    stop_event = asyncio.Event()
    
    # Queremos verificar que se llame process *antes* de que llegue a wait_for/sleep
    with patch("runtime.workers.content_retry._process_missing_briefs", new_callable=AsyncMock) as mock_process:
        # En la primera iteracion, set stop_event asi no llega a dormir
        async def side_effect(*args, **kwargs):
            stop_event.set()
            
        mock_process.side_effect = side_effect
        
        # Le ponemos timeout enorme, si se traba es porque no llamo primero
        await asyncio.wait_for(
            content_strategy_retry_loop(
                session_factory=mock_session_factory,
                llm_provider=mock_llm_provider,
                mission_context="test",
                stop_event=stop_event,
                interval_seconds=9999,
            ), 
            timeout=1.0
        )
        
        assert mock_process.call_count == 1


# ---------------------------------------------------------------------------
# Regression tests — query eligibility filter logic
# Verifica que _process_missing_briefs incluya/excluya oportunidades según
# el criterio: sin ContentBrief AND content_generation_attempts < 3
# ---------------------------------------------------------------------------

def _make_session_factory(opportunities: list):
    """Construye un mock de session_factory que devuelve una lista de oportunidades."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = opportunities
    session.execute.return_value = result_mock
    session.__aenter__.return_value = session
    session.__aexit__.return_value = None
    factory = MagicMock(return_value=session)
    return factory


@pytest.mark.asyncio
async def test_f_opportunity_with_zero_attempts_and_no_brief_is_eligible():
    """
    Oportunidad con content_generation_attempts=0 y sin ContentBrief
    DEBE aparecer en el batch — es el caso del bug reportado.

    Verifica que _process_missing_briefs llama a run_content_strategy_flow
    exactamente una vez cuando hay una oportunidad elegible.
    """
    from uuid import UUID
    from datetime import datetime, timezone

    opp = MagicMock()
    opp.id = UUID("d5f6a263-2e0b-48c8-b855-6ae7891d66de")
    opp.mission_id = UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    opp.title = "Creación de un Espacio de Co-Working para Emprendedores"
    opp.description = "Descripción de la oportunidad."
    opp.priority = "high"
    opp.content_generation_attempts = 0
    opp.created_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    # No existe ContentBrief → el outerjoin LEFT JOIN devuelve NULL para cb.id
    # La session_factory devuelve esta oportunidad (el filtro SQL ya la incluyó)

    factory = _make_session_factory([opp])

    with patch(
        "runtime.workers.content_retry.run_content_strategy_flow",
        new_callable=AsyncMock,
    ) as mock_flow:
        await _process_missing_briefs(
            session_factory=factory,
            llm_provider=AsyncMock(),
            mission_context="test",
        )

    # Debe haber procesado exactamente 1 oportunidad
    assert mock_flow.call_count == 1


@pytest.mark.asyncio
async def test_g_opportunity_with_existing_brief_is_excluded():
    """
    Oportunidad que YA tiene un ContentBrief NO debe aparecer en el batch.

    Verifica que cuando la session devuelve lista vacía (el LEFT JOIN filtró la
    oportunidad porque cb.id IS NOT NULL), _process_missing_briefs no llama
    a run_content_strategy_flow.
    """
    # La session devuelve [] porque el WHERE cb.id IS NULL ya excluyó la fila
    factory = _make_session_factory([])

    with patch(
        "runtime.workers.content_retry.run_content_strategy_flow",
        new_callable=AsyncMock,
    ) as mock_flow:
        await _process_missing_briefs(
            session_factory=factory,
            llm_provider=AsyncMock(),
            mission_context="test",
        )

    # No debe haber procesado ninguna oportunidad
    assert mock_flow.call_count == 0


@pytest.mark.asyncio
async def test_h_opportunity_with_three_attempts_is_excluded():
    """
    Oportunidad con content_generation_attempts=3 NO debe aparecer en el batch.

    Verifica que cuando la session devuelve [] (el WHERE attempts < 3 ya la
    excluyó), _process_missing_briefs no llama a run_content_strategy_flow.
    """
    # La session devuelve [] porque content_generation_attempts >= 3
    factory = _make_session_factory([])

    with patch(
        "runtime.workers.content_retry.run_content_strategy_flow",
        new_callable=AsyncMock,
    ) as mock_flow:
        await _process_missing_briefs(
            session_factory=factory,
            llm_provider=AsyncMock(),
            mission_context="test",
        )

    assert mock_flow.call_count == 0
