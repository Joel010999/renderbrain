import os
import subprocess
import urllib.request
import urllib.error
import base64
from typing import Optional

def get_env_var(name: str, default: Optional[str] = None) -> str:
    from runtime.shared.config import settings
    # We can rely on settings for credentials
    return getattr(settings, name, default)

def run_http(url: str, username: str = None, password: str = None) -> tuple[int, str]:
    req = urllib.request.Request(url)
    if username and password:
        auth = f"{username}:{password}"
        b64_auth = base64.b64encode(auth.encode('utf-8')).decode('ascii')
        req.add_header("Authorization", f"Basic {b64_auth}")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.getcode(), response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.code, str(e)
    except Exception as e:
        return 0, str(e)

class SkipTest(Exception):
    pass

def test_health():
    code, _ = run_http("http://localhost:8000/health")
    assert code == 200, f"/health failed with code {code}"
    print("[OK] /health returned 200")

def test_ready():
    code, _ = run_http("http://localhost:8000/ready")
    assert code == 200, f"/ready failed with code {code}"
    print("[OK] /ready returned 200")

def test_auth_protected():
    code, _ = run_http("http://localhost:8000/api/v1/missions")
    assert code == 401, f"Expected 401 for protected route, got {code}"
    print("[OK] /api/v1/missions without auth returned 401")

def test_auth_success():
    from runtime.shared.config import settings
    username = settings.RENDERBRAIN_ADMIN_USERNAME
    if not username or not settings.RENDERBRAIN_ADMIN_PASSWORD:
        raise SkipTest("Credentials not in environment")
    password = settings.RENDERBRAIN_ADMIN_PASSWORD.get_secret_value()
    code, _ = run_http("http://localhost:8000/api/v1/missions", username, password)
    assert code == 200, f"Expected 200 with auth, got {code}"
    print("[OK] /api/v1/missions with auth returned 200")

def test_redis_reachable():
    from runtime.shared.config import settings
    import redis
    
    r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    assert r.ping() is True, "Redis is not reachable"
    print("[OK] Redis ping successful")

def test_postgres_reachable():
    import asyncio
    from runtime.infrastructure.database.session import async_session
    from sqlalchemy import text
    
    async def ping_db():
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
    
    asyncio.run(ping_db())
    print("[OK] PostgreSQL ping successful")

def test_migrations():
    # Run alembic current and alembic heads
    current = subprocess.check_output(["uv", "run", "alembic", "current"], text=True).strip()
    heads = subprocess.check_output(["uv", "run", "alembic", "heads"], text=True).strip()
    
    curr_id = current.split(' ')[0] if current else None
    head_id = heads.split(' ')[0] if heads else None
    
    assert curr_id == head_id, f"Migrations not at head: current={curr_id}, head={head_id}"
    print(f"[OK] Migrations are at head ({head_id})")

def main():
    print("Running RenderBrain v1 Smoke Test...")
    tests = [
        test_health,
        test_ready,
        test_auth_protected,
        test_auth_success,
        test_redis_reachable,
        test_postgres_reachable,
        test_migrations,
    ]
    
    passed = 0
    failed = 0
    skipped = 0
    for test in tests:
        try:
            test()
            passed += 1
        except SkipTest as e:
            print(f"[SKIP] {test.__name__}: {e}")
            skipped += 1
        except Exception as e:
            print(f"[FAIL] {test.__name__}: {e}")
            failed += 1
            
    print("\n--- Smoke Test Summary ---")
    print(f"{passed} passed")
    print(f"{failed} failed")
    print(f"{skipped} skipped")
    
    if failed > 0:
        exit(1)

if __name__ == "__main__":
    main()
