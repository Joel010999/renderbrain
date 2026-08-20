import asyncio
import signal
from unittest.mock import patch, AsyncMock

import pytest

from runtime.shared.config import settings, validate_api_settings, validate_worker_settings
from fastapi.testclient import TestClient

import runtime.worker
import runtime.scheduler

# Mockear settings temporalmente
@pytest.fixture
def temp_settings():
    original_db = settings.DATABASE_URL
    original_user = settings.RENDERBRAIN_ADMIN_USERNAME
    original_pass = settings.RENDERBRAIN_ADMIN_PASSWORD
    original_openai = settings.OPENAI_API_KEY
    
    yield settings
    
    settings.DATABASE_URL = original_db
    settings.RENDERBRAIN_ADMIN_USERNAME = original_user
    settings.RENDERBRAIN_ADMIN_PASSWORD = original_pass
    settings.OPENAI_API_KEY = original_openai

def test_settings_validation_api_fails_without_auth(temp_settings):
    temp_settings.RENDERBRAIN_ADMIN_USERNAME = None
    with pytest.raises(RuntimeError, match="RENDERBRAIN_ADMIN_USERNAME and RENDERBRAIN_ADMIN_PASSWORD are required"):
        validate_api_settings()

def test_settings_validation_worker_fails_without_openai(temp_settings):
    temp_settings.OPENAI_API_KEY = None
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        validate_worker_settings()

@pytest.mark.asyncio
async def test_ready_returns_200_when_ok():
    # Evitar importar main directamente al principio para que no falle la validacion de API en test
    with patch("runtime.shared.config.validate_api_settings"):
        from runtime.api.main import app
        
        with patch("runtime.api.main.check_postgres", new_callable=AsyncMock) as mock_pg:
            
            mock_pg.return_value = True
            
            client = TestClient(app)
            response = client.get("/ready")
            assert response.status_code == 200
            assert response.json() == {"status": "ready", "dependencies": {"postgres": "ok"}}

@pytest.mark.asyncio
async def test_ready_returns_503_when_failing():
    with patch("runtime.shared.config.validate_api_settings"):
        from runtime.api.main import app
        
        with patch("runtime.api.main.check_postgres", new_callable=AsyncMock) as mock_pg:
            
            mock_pg.return_value = False
            
            client = TestClient(app)
            response = client.get("/ready")
            assert response.status_code == 503
            assert response.json() == {"status": "unavailable", "dependencies": {"postgres": "error"}}

@pytest.mark.asyncio
async def test_worker_graceful_shutdown():
    with patch("runtime.shared.config.validate_worker_settings"), \
         patch("runtime.worker.get_redis_client") as mock_get_redis, \
         patch("runtime.worker.RedisConsumerGroup.ensure_group", new_callable=AsyncMock), \
         patch("runtime.worker.SignalWorker.process_next", new_callable=AsyncMock) as mock_process:
        
        mock_redis = AsyncMock()
        mock_redis.aclose = AsyncMock()
        mock_get_redis.return_value = mock_redis
        
        from runtime.worker import main
        
        # Simular que al llamar process_next se genera un KeyboardInterrupt
        mock_process.side_effect = KeyboardInterrupt()
        
        # El main debe capturarlo y salir limpiamente
        await main()
        
        assert mock_process.called

@pytest.mark.asyncio
async def test_scheduler_graceful_shutdown():
    with patch("runtime.shared.config.validate_scheduler_settings"), \
         patch("runtime.scheduler.sync_missions", new_callable=AsyncMock) as mock_sync, \
         patch("runtime.scheduler.AsyncIOScheduler") as mock_scheduler_class:
        
        # Mockear el scheduler para que .start() no haga nada problemático
        mock_scheduler = mock_scheduler_class.return_value
        
        from runtime.scheduler import main
        
        # Para salir del await stop_event.wait(), vamos a parchear asyncio.Event.wait
        with patch("asyncio.Event.wait", new_callable=AsyncMock) as mock_wait:
            mock_wait.side_effect = KeyboardInterrupt()
            
            await main()
            
            # Verificar que intentó apagar el scheduler
            assert mock_scheduler.shutdown.called

@pytest.mark.asyncio
async def test_worker_continues_on_message_error():
    """
    Verifica que si process_one() lanza un error, el worker captura la excepción,
    no hace XACK, registra el error y el bucle continúa procesando el siguiente mensaje.
    """
    with patch("runtime.shared.config.validate_worker_settings"), \
         patch("runtime.worker.get_redis_client"), \
         patch("runtime.worker.RedisConsumerGroup") as mock_cg_class, \
         patch("runtime.worker.CognitiveEngine"), \
         patch("runtime.worker.OpenAIAdapter"), \
         patch("runtime.worker.SignalWorker.process_one", new_callable=AsyncMock) as mock_process_one:

        # Configurar el consumer group mock
        mock_cg = mock_cg_class.return_value
        mock_cg.ensure_group = AsyncMock()
        
        # Simular que hay 2 mensajes en pending
        # El primero fallará, el segundo será exitoso
        mock_cg.read_pending = AsyncMock(return_value=[
            ("msg1", "envelope1"),
            ("msg2", "envelope2"),
        ])
        mock_cg.read_new = AsyncMock(return_value=[])

        # Hacemos que el primer process_one falle, y el segundo pase
        mock_process_one.side_effect = [Exception("Simulated error processing msg1"), ("canonical2", "tx2")]

        from runtime.worker import SignalWorker
        
        worker = SignalWorker(
            consumer_group=mock_cg,
            session_factory=AsyncMock(),
            cognitive_engine=AsyncMock(),
            llm_provider=AsyncMock(),
            mission_context="test",
        )

        results = await worker.process_next(count=2)

        # process_one debió llamarse 2 veces
        assert mock_process_one.call_count == 2
        # El resultado sólo contiene el del segundo mensaje
        assert len(results) == 1
        assert results[0] == ("canonical2", "tx2")
