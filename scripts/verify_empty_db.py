import asyncio
import subprocess
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from runtime.shared.config import settings

TEST_DB_NAME = "renderbrain_empty_test"

async def create_db(engine_url, db_name):
    default_url = engine_url.rsplit('/', 1)[0] + "/postgres"
    engine = create_async_engine(default_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {db_name}"))
        await conn.execute(text(f"CREATE DATABASE {db_name}"))
    await engine.dispose()

async def main():
    print(f"Creating empty temporal DB: {TEST_DB_NAME}")
    await create_db(settings.DATABASE_URL, TEST_DB_NAME)
    
    # Run alembic upgrade head using the temporal DB URL
    target_url = settings.DATABASE_URL.rsplit('/', 1)[0] + f"/{TEST_DB_NAME}"
    env = os.environ.copy()
    env["ALEMBIC_DB_URL"] = target_url  # the alembic.ini or env.py usually reads DATABASE_URL but we override it
    # Wait, in this project it's DATABASE_URL that alembic reads!
    env["DATABASE_URL"] = target_url
    
    print("Running alembic upgrade head on empty DB...")
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], env=env, check=True)
    
    print("Running alembic current...")
    current = subprocess.run(["uv", "run", "alembic", "current"], env=env, capture_output=True, text=True, check=True)
    print(f"Current:\n{current.stdout.strip()}")
    
    print("Running alembic heads...")
    heads = subprocess.run(["uv", "run", "alembic", "heads"], env=env, capture_output=True, text=True, check=True)
    print(f"Heads:\n{heads.stdout.strip()}")
    
    print("[OK] Empty DB migration verification successful.")

if __name__ == "__main__":
    asyncio.run(main())
