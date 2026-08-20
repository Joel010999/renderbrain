import asyncio
import subprocess
import os
from uuid import uuid4
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from runtime.shared.config import settings

TEST_DB_NAME = "renderbrain_restore_test"
DUMMY_ID = uuid4()
DUMMY_TEXT = f"Test Recovery {DUMMY_ID}"

async def create_db(engine_url, db_name):
    default_url = engine_url.rsplit('/', 1)[0] + "/postgres"
    engine = create_async_engine(default_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))
        await conn.execute(text(f"CREATE DATABASE {db_name}"))
    await engine.dispose()

async def insert_dummy_data():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    
    async with async_session() as session:
        # 1. Mission
        await session.execute(text("""
            INSERT INTO missions (id, name, source, target, enabled, interval_seconds, created_at, updated_at)
            VALUES (:id, :target, 'test', :target, true, 60, :now, :now)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID, "target": DUMMY_TEXT, "now": datetime.now(UTC)})
        
        # 2. CanonicalSignal
        await session.execute(text("""
            INSERT INTO canonical_signals (id, mission_id, source_event_id, source, sensor, content, captured_at, normalized_at)
            VALUES (:id, :id, :id, 'test', 'test', :content, :now, :now)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID, "content": DUMMY_TEXT, "now": datetime.now(UTC)})
        
        # 3. ProcessedSignal
        await session.execute(text("""
            INSERT INTO processed_signals (id, mission_id, source, fingerprint, processed_at)
            VALUES (:id, :id, 'test', :ext_id, :now)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID, "ext_id": str(DUMMY_ID), "now": datetime.now(UTC)})
        
        # 4. Evidence
        await session.execute(text("""
            INSERT INTO evidence (id, mission_id, canonical_signal_id, content, created_at)
            VALUES (:id, :id, :id, :content, :now)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID, "content": DUMMY_TEXT, "now": datetime.now(UTC)})
        
        # 5. Insight
        await session.execute(text("""
            INSERT INTO insights (id, mission_id, evidence_id, content, created_at)
            VALUES (:id, :id, :id, :content, :now)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID, "content": DUMMY_TEXT, "now": datetime.now(UTC)})
        
        # 6. Pattern
        await session.execute(text("""
            INSERT INTO patterns (id, mission_id, content, support_count, created_at)
            VALUES (:id, :id, :content, 1, :now)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID, "content": DUMMY_TEXT, "now": datetime.now(UTC)})
        
        # 7. Opportunity
        await session.execute(text("""
            INSERT INTO opportunities (id, mission_id, content, created_at)
            VALUES (:id, :id, :content, :now)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID, "content": DUMMY_TEXT, "now": datetime.now(UTC)})
        
        # Relations: pattern_insights and opportunity_patterns
        await session.execute(text("""
            INSERT INTO pattern_insights (pattern_id, insight_id)
            VALUES (:id, :id)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID})
        
        await session.execute(text("""
            INSERT INTO opportunity_patterns (opportunity_id, pattern_id)
            VALUES (:id, :id)
            ON CONFLICT DO NOTHING
        """), {"id": DUMMY_ID})
        
        await session.commit()
    await engine.dispose()
    print(f"[OK] Dummy data inserted into source DB (ID={DUMMY_ID})")

async def verify_restore():
    restore_url = settings.DATABASE_URL.rsplit('/', 1)[0] + f"/{TEST_DB_NAME}"
    engine = create_async_engine(restore_url)
    
    async with engine.connect() as conn:
        # Check alembic_version
        result = await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        alembic_head = result.scalar()
        print(f"[*] Alembic Version after restore: {alembic_head}")
        assert alembic_head is not None, "alembic_version is missing!"
        
        # Check Mission
        result = await conn.execute(text("SELECT target FROM missions WHERE id = :id"), {"id": DUMMY_ID})
        assert result.scalar() == DUMMY_TEXT, "Mission missing or mismatched"
        
        # Check CanonicalSignal
        result = await conn.execute(text("SELECT content FROM canonical_signals WHERE id = :id"), {"id": DUMMY_ID})
        assert result.scalar() == DUMMY_TEXT, "CanonicalSignal missing"
        
        # Check ProcessedSignal
        result = await conn.execute(text("SELECT source FROM processed_signals WHERE mission_id = :id"), {"id": DUMMY_ID})
        assert result.scalar() == "test", "ProcessedSignal missing"
        
        # Check Relations (Pattern <-> Insight)
        result = await conn.execute(text("SELECT pattern_id, insight_id FROM pattern_insights WHERE pattern_id = :id"), {"id": DUMMY_ID})
        row = result.first()
        assert row is not None and row[0] == DUMMY_ID and row[1] == DUMMY_ID, "Pattern-Insight relation missing"
        
        # Check Relations (Opportunity <-> Pattern)
        result = await conn.execute(text("SELECT opportunity_id, pattern_id FROM opportunity_patterns WHERE opportunity_id = :id"), {"id": DUMMY_ID})
        row = result.first()
        assert row is not None and row[0] == DUMMY_ID and row[1] == DUMMY_ID, "Opportunity-Pattern relation missing"
        
    await engine.dispose()
    print(f"[OK] Post-restore validation successful. All entities and relations match ({DUMMY_ID})")

async def main():
    print("Starting Restore Verification...")
    await insert_dummy_data()
    
    print("Running backup...")
    subprocess.run(["cmd.exe", "/c", "scripts\\backup_db.bat"], check=True)
    
    print("Validating backup file format...")
    list_result = subprocess.run(["cmd.exe", "/c", "docker exec -i renderbrain-postgres pg_restore -U renderbrain --list < renderbrain_backup.dump"], capture_output=True, text=True)
    assert "alembic_version" in list_result.stdout, "pg_restore --list did not find alembic_version (invalid dump)"
    print("[OK] Backup file is a valid custom archive")
    
    print(f"Creating temporal DB: {TEST_DB_NAME}")
    await create_db(settings.DATABASE_URL, TEST_DB_NAME)
    
    print(f"Restoring to {TEST_DB_NAME}...")
    subprocess.run(["cmd.exe", "/c", f"docker exec -i renderbrain-postgres pg_restore -U renderbrain -d {TEST_DB_NAME} --clean --if-exists --no-owner -1 < renderbrain_backup.dump"], check=True)
    
    await verify_restore()
    print("--- Restore Verification Complete ---")

if __name__ == "__main__":
    asyncio.run(main())
