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
