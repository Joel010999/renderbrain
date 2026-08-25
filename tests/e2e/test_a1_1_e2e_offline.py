"""
tests/e2e/test_a1_1_e2e_offline.py

E2E Offline — A1.1 Instagram Daily Collector

Flujo verificado (CON DB+Redis reales, SIN Apify real, SIN OpenAI/LLM):
    1ª Corrida:
        Profile devuelve: Reel A, Post B, Story C
        → 3 RawSignalDetected con mismo mission_id
        → 3 EventEnvelopes independientes en Redis Stream
        → run_signal_flow() × 3 → 3 CanonicalSignal en PostgreSQL
        → compute_fingerprint() × 3 + ProcessedSignalRepository.add() × 3
        → 3 ProcessedSignal durable

    2ª Corrida:
        Profile devuelve: Reel A, Post B, Story C, Post D
        → A/B/C: ProcessedSignalRepository.exists() → HIT → SKIP (sin procesamiento)
        → D: HIT-MISS → procesado → 1 nuevo CanonicalSignal + ProcessedSignal
        → TOTAL: 4 ProcessedSignal al final

Requisito: docker compose up -d (PostgreSQL + Redis corriendo)
    uv run pytest tests/e2e/test_a1_1_e2e_offline.py -v -m integration
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete

from runtime.contracts.canonical_signal import CanonicalSignal
from runtime.contracts.event_envelope import EventEnvelope
from runtime.contracts.processed_signal import ProcessedSignal
from runtime.contracts.raw_signal_detected import RawSignalDetected
from runtime.engines.sensors.instagram_profile import InstagramProfileSensor
from runtime.events.bus import RedisEventBus
from runtime.events.publish_signal import wrap_and_publish
from runtime.infrastructure.database import async_session
from runtime.infrastructure.database.models.canonical_signal import CanonicalSignalModel
from runtime.infrastructure.database.models.mission import ProcessedSignalModel
from runtime.infrastructure.database.repositories.canonical_signal import CanonicalSignalRepository
from runtime.infrastructure.database.repositories.processed_signal import ProcessedSignalRepository
from runtime.infrastructure.redis.client import get_redis_client
from runtime.orchestration.signal_flow import run_signal_flow
from runtime.workers.fingerprint import compute_fingerprint

_TEST_STREAM = "renderbrain:test:events:a1.1:e2e"


# ---------------------------------------------------------------------------
# FakeAdapter — sin llamadas a Apify real
# ---------------------------------------------------------------------------

def _make_post_item(native_id: str, username: str = "testprofile") -> dict:
    return {
        "id": native_id,
        "shortCode": f"SC{native_id}",
        "caption": f"Caption {native_id}",
        "ownerUsername": username,
        "ownerFullName": "Test Profile",
        "ownerId": "111222333",
        "likesCount": 50,
        "commentsCount": 3,
        "timestamp": "2026-08-25T00:00:00+00:00",
    }


def _make_story_item(story_id: str, username: str = "testprofile") -> dict:
    return {
        "id": story_id,
        "storyId": story_id,
        "ownerUsername": username,
        "timestamp": "2026-08-25T01:00:00+00:00",
    }


class FakeProfileAdapter:
    """FakeAdapter de perfil: no llama a Apify."""
    def __init__(self, posts: list[dict], reels: list[dict], stories: list[dict]):
        self._posts = posts
        self._reels = reels
        self._stories = stories

    def fetch_profile_posts(self, username: str, limit: int = 10, results_type: str = "posts") -> list[dict]:
        if results_type == "reels":
            return self._reels[:limit]
        return self._posts[:limit]

    def fetch_profile_stories(self, username: str, limit: int = 20) -> list[dict]:
        return self._stories[:limit]


# ---------------------------------------------------------------------------
# E2E Test — Primera + Segunda corrida completo
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_a1_1_e2e_first_and_second_run():
    """
    E2E completo de A1.1 con DB+Redis reales y FakeAdapter (sin Apify/OpenAI).

    Primera corrida:
        Reel A, Post B, Story C
        → 3 RawSignalDetected mismo mission_id
        → 3 EventEnvelopes en Redis
        → run_signal_flow → 3 CanonicalSignal en PostgreSQL
        → 3 ProcessedSignal duraderos

    Segunda corrida:
        Reel A, Post B, Story C, Post D
        → A/B/C: ProcessedSignalRepository.exists() → HIT → skip
        → D: MISS → procesado
        → Total ProcessedSignal: 4
    """
    redis = get_redis_client()
    await redis.delete(_TEST_STREAM)

    mission_id = uuid4()
    canonical_ids: list = []

    try:
        # =====================================================================
        # PRIMERA CORRIDA: Reel A + Post B + Story C
        # =====================================================================
        first_adapter = FakeProfileAdapter(
            posts=[_make_post_item("PostB")],
            reels=[_make_post_item("ReelA")],
            stories=[_make_story_item("StoryC")],
        )
        sensor1 = InstagramProfileSensor(
            mission_id=mission_id,
            username="testprofile",
            adapter=first_adapter,
        )

        # 1a. detect() → Posts + Reels
        raw_posts_reels = await sensor1.detect()
        assert len(raw_posts_reels) == 2, f"Expected 2 (1 post + 1 reel), got {len(raw_posts_reels)}"

        # 1b. detect_stories() → Story C
        raw_stories = await sensor1.detect_stories()
        assert len(raw_stories) == 1, f"Expected 1 story, got {len(raw_stories)}"

        all_first = raw_posts_reels + raw_stories
        assert len(all_first) == 3

        # Verificar: todos tienen mismo mission_id
        for s in all_first:
            assert s.mission_id == mission_id, f"mission_id mismatch: {s.mission_id}"

        # Verificar: content_types correctos
        content_types_first = {s.raw_payload["content_type"] for s in all_first}
        assert content_types_first == {"post", "reel", "story"}, f"Expected post/reel/story, got {content_types_first}"

        # 1c. Publicar 3 EventEnvelopes independientes en Redis
        bus = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
        envelopes_first: list[EventEnvelope] = []
        for raw in all_first:
            env = await wrap_and_publish(raw, bus)
            envelopes_first.append(env)

        assert len(envelopes_first) == 3, f"Expected 3 envelopes, got {len(envelopes_first)}"

        # Verificar que están en Redis
        events_in_stream = await bus.read(count=10, last_id="0-0")
        assert len(events_in_stream) == 3, f"Expected 3 events in stream, got {len(events_in_stream)}"

        # 1d. run_signal_flow → 3 CanonicalSignal en PostgreSQL
        async with async_session() as session:
            try:
                for envelope in envelopes_first:
                    canonical: CanonicalSignal = await run_signal_flow(
                        envelope=envelope,
                        session=session,
                    )
                    canonical_ids.append(canonical.id)

                    # Verificar campos críticos
                    assert canonical.mission_id == mission_id
                    assert canonical.content_type in {"post", "reel", "story"}, \
                        f"content_type invalid: {canonical.content_type}"
                    assert canonical.source_account_username == "testprofile", \
                        f"source_account_username: {canonical.source_account_username}"
                    assert canonical.source_event_id is not None

                await session.commit()
            except Exception:
                await session.rollback()
                raise

        assert len(canonical_ids) == 3, f"Expected 3 CanonicalSignals, got {len(canonical_ids)}"

        # 1e. Registrar 3 ProcessedSignal (fingerprints de A/B/C)
        fps_abc: set[str] = set()
        async with async_session() as session:
            try:
                repo_ps = ProcessedSignalRepository(session)
                for raw in all_first:
                    fp = compute_fingerprint(raw)
                    fps_abc.add(fp)
                    ps = ProcessedSignal(
                        mission_id=mission_id,
                        source="instagram",
                        fingerprint=fp,
                    )
                    await repo_ps.add(ps)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        assert len(fps_abc) == 3, f"Expected 3 unique fingerprints, got {len(fps_abc)}"
        # Verificar namespace de stories
        story_fps = [fp for fp in fps_abc if fp.startswith("instagram:story:")]
        assert len(story_fps) == 1, f"Expected 1 story fingerprint, got {story_fps}"

        # =====================================================================
        # SEGUNDA CORRIDA: Reel A + Post B + Story C + Post D
        # =====================================================================
        second_adapter = FakeProfileAdapter(
            posts=[_make_post_item("PostB"), _make_post_item("PostD")],
            reels=[_make_post_item("ReelA")],
            stories=[_make_story_item("StoryC")],
        )
        sensor2 = InstagramProfileSensor(
            mission_id=mission_id,
            username="testprofile",
            adapter=second_adapter,
        )

        raw_posts_reels2 = await sensor2.detect()
        raw_stories2 = await sensor2.detect_stories()
        all_second = raw_posts_reels2 + raw_stories2
        assert len(all_second) == 4, f"Expected 4 signals, got {len(all_second)}"

        # 2a. Clasificar: existentes (HIT) vs nuevos (MISS)
        new_signals = []
        hit_signals = []
        async with async_session() as session:
            repo_ps = ProcessedSignalRepository(session)
            for raw in all_second:
                fp = compute_fingerprint(raw)
                already = await repo_ps.exists(
                    mission_id=mission_id,
                    source="instagram",
                    fingerprint=fp,
                )
                if already:
                    hit_signals.append((raw, fp))
                else:
                    new_signals.append((raw, fp))

        # Verificar: A/B/C son HIT, solo D es MISS
        assert len(hit_signals) == 3, \
            f"Expected 3 HITs (A/B/C), got {len(hit_signals)}: {[fp for _, fp in hit_signals]}"
        assert len(new_signals) == 1, \
            f"Expected 1 MISS (D only), got {len(new_signals)}: {[fp for _, fp in new_signals]}"

        # Verificar que el nuevo es PostD
        new_fp = new_signals[0][1]
        assert new_fp == "instagram:PostD", f"Expected 'instagram:PostD', got '{new_fp}'"

        # 2b. Procesar solo D (sin llamada cognitiva para A/B/C)
        new_canonical_id = None
        async with async_session() as session:
            try:
                # Publicar solo D
                new_raw, _ = new_signals[0]
                bus2 = RedisEventBus(redis_client=redis, stream=_TEST_STREAM)
                env_d = await wrap_and_publish(new_raw, bus2)

                # run_signal_flow para D
                canonical_d: CanonicalSignal = await run_signal_flow(
                    envelope=env_d,
                    session=session,
                )
                new_canonical_id = canonical_d.id
                canonical_ids.append(new_canonical_id)

                assert canonical_d.mission_id == mission_id
                assert canonical_d.native_id == "PostD", \
                    f"Expected native_id='PostD', got '{canonical_d.native_id}'"

                # Registrar ProcessedSignal para D
                repo_ps = ProcessedSignalRepository(session)
                ps_d = ProcessedSignal(
                    mission_id=mission_id,
                    source="instagram",
                    fingerprint=new_fp,
                )
                await repo_ps.add(ps_d)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        # 2c. Verificar total de ProcessedSignal = 4 (A + B + C + D)
        async with async_session() as session:
            from sqlalchemy import select, func
            from runtime.infrastructure.database.models.mission import ProcessedSignalModel
            result = await session.execute(
                select(func.count()).select_from(ProcessedSignalModel).where(
                    ProcessedSignalModel.mission_id == mission_id
                )
            )
            total_ps = result.scalar()

        assert total_ps == 4, f"Expected 4 total ProcessedSignals, got {total_ps}"

    finally:
        # ------------------------------------------------------------------
        # LIMPIEZA: eliminar todas las filas del test
        # ------------------------------------------------------------------
        async with async_session() as session:
            await session.execute(
                delete(ProcessedSignalModel).where(
                    ProcessedSignalModel.mission_id == mission_id
                )
            )
            for cid in canonical_ids:
                await session.execute(
                    delete(CanonicalSignalModel).where(CanonicalSignalModel.id == cid)
                )
            await session.commit()

        await redis.delete(_TEST_STREAM)
        await redis.aclose()
