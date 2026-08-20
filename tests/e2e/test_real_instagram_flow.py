"""
tests/e2e/test_real_instagram_flow.py

Test E2E S2.4 — First Real Sensor Flow completo.
CONSUME CRÉDITOS: ejecutar solo cuando sea necesario.

Flujo verificado (sin mocks, 1 sola llamada a Apify):
    Input URL real de Instagram -> ApifyInstagramAdapter (limit=1)
        → InstagramSensor.detect()        → RawSignalDetected
        → wrap_and_publish(signal, bus)   → EventEnvelope (publicado en Redis)
        → bus.read()                      → EventEnvelope reconstruido
        → run_signal_flow(envelope, session)
            → RawSignalDetected (reconstruido)
            → NormalizerEngine.normalize()  → CanonicalSignal base
            → model_copy(source_event_id=envelope.event_id) → trazabilidad real
            → CanonicalSignalRepository.save() → PostgreSQL
        → repository.get_by_id()          → CanonicalSignal leído de DB

Requisito previo: contenedores renderbrain-postgres y renderbrain-redis corriendo y
APIFY_API_TOKEN configurado en .env.

    docker compose up -d
    uv run pytest --run-external -m external tests/e2e/test_real_instagram_flow.py -v
"""

import pytest
from uuid import UUID, uuid4
from sqlalchemy import delete

from runtime.shared.config import settings
from runtime.contracts import CanonicalSignal, RawSignalDetected
from runtime.contracts.event_envelope import EventEnvelope
from runtime.engines.sensors import InstagramSensor
from runtime.events import EVENT_TYPE, RedisEventBus, wrap_and_publish
from runtime.infrastructure.apify.adapter import ApifyInstagramAdapter
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.repositories import CanonicalSignalRepository
from runtime.infrastructure.redis.client import get_redis_client
from runtime.orchestration.signal_flow import run_signal_flow

_TEST_URL = "https://www.instagram.com/p/B8rk0ISnDT5/"
_TEST_STREAM = "renderbrain:test:events:s2:e2e:instagram"


@pytest.mark.external
@pytest.mark.integration
async def test_real_instagram_flow_end_to_end():
    """
    Flujo E2E completo: Instagram Real → Apify → Sensor → Redis → NormalizerEngine → PostgreSQL.
    """
    if settings.APIFY_API_TOKEN is None:
        pytest.skip("APIFY_API_TOKEN no configurado en .env — test externo omitido.")

    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    canonical_id: UUID | None = None

    try:
        # ------------------------------------------------------------------
        # 1. InstagramSensor → ApifyInstagramAdapter → RawSignalDetected
        # ------------------------------------------------------------------
        mission_id: UUID = uuid4()
        
        # Una sola llamada real a Apify con limit=1
        adapter = ApifyInstagramAdapter()
        sensor = InstagramSensor(
            mission_id=mission_id, 
            url=_TEST_URL, 
            adapter=adapter
        )
        
        raw_signal: RawSignalDetected = await sensor.detect()

        assert raw_signal.sensor == "instagram_apify_sensor"
        assert raw_signal.source == "instagram"
        assert raw_signal.mission_id == mission_id
        assert raw_signal.captured_at is not None
        assert "data" in raw_signal.raw_payload
        assert raw_signal.raw_payload["items_received"] == 1

        # ------------------------------------------------------------------
        # 2. wrap_and_publish → Redis Stream
        # ------------------------------------------------------------------
        bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
        published: EventEnvelope = await wrap_and_publish(raw_signal, bus)

        assert published.event_type == EVENT_TYPE
        assert published.event_id is not None
        assert published.correlation_id == published.event_id

        # ------------------------------------------------------------------
        # 3. Leer de vuelta desde Redis Stream
        # ------------------------------------------------------------------
        events = await bus.read(count=1, last_id="0-0")
        assert len(events) == 1
        envelope: EventEnvelope = events[0]
        assert envelope.event_id == published.event_id

        # ------------------------------------------------------------------
        # 4. Orquestación: run_signal_flow → normalizar → persistir
        # ------------------------------------------------------------------
        async with async_session() as session:
            try:
                canonical: CanonicalSignal = await run_signal_flow(
                    envelope=envelope,
                    session=session,
                )
                await session.commit()
                canonical_id = canonical.id

                # ----------------------------------------------------------
                # 5. Leer desde PostgreSQL vía repository
                # ----------------------------------------------------------
                repo = CanonicalSignalRepository(session)
                recovered: CanonicalSignal | None = await repo.get_by_id(canonical_id)

                assert recovered is not None
                assert isinstance(recovered, CanonicalSignal)
                assert not isinstance(recovered, RawSignalDetected)

                # 6. Verificaciones estrictas
                assert isinstance(recovered.id, UUID)
                assert recovered.id == canonical.id
                assert recovered.mission_id == mission_id
                
                # Genealogía
                assert recovered.source_event_id == envelope.event_id
                
                # Origen
                assert recovered.source == "instagram"
                assert recovered.sensor == "instagram_apify_sensor"
                
                # Campos mapeados por Normalizer
                # Sabiendo que es un post válido, esperamos content o autor (o strings vacías / None si fallback)
                assert isinstance(recovered.content, str)
                assert recovered.captured_at is not None
                assert recovered.captured_at.tzinfo is not None
                assert recovered.normalized_at is not None
                assert recovered.normalized_at.tzinfo is not None
                
            finally:
                # Limpieza de base de datos
                await session.rollback()
                if canonical_id is not None:
                    await session.execute(
                        delete(CanonicalSignalModel).where(
                            CanonicalSignalModel.id == canonical_id
                        )
                    )
                    await session.commit()

    finally:
        # Limpieza de Redis
        await redis.delete(_TEST_STREAM)
        await redis.aclose()
