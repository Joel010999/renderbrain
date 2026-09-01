import asyncio

from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.responses import JSONResponse

from runtime.infrastructure.database.probes import check_postgres
from runtime.infrastructure.redis.probes import check_redis

from runtime.api.routes import missions, intelligence, dashboard, content_briefs
from runtime.api.auth import get_current_admin
from runtime.shared.config import validate_api_settings

@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_api_settings()
    yield

app = FastAPI(lifespan=lifespan)
app.include_router(dashboard.router)
app.include_router(missions.router, prefix="/api/v1", dependencies=[Depends(get_current_admin)])
app.include_router(intelligence.router, prefix="/api/v1", dependencies=[Depends(get_current_admin)])
app.include_router(content_briefs.router, prefix="/api/v1", dependencies=[Depends(get_current_admin)])


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/ready")
async def readiness_check():
    """
    Comprueba la disponibilidad operativa de las dependencias críticas de la API (PostgreSQL).
    La API no utiliza Redis directamente.

    Returns:
        200 — {"status": "ready", "dependencies": {"postgres": "ok"}}
        503 — {"status": "unavailable", "dependencies": {"postgres": "error"}}
    """
    postgres_ok = await check_postgres()

    dependencies = {
        "postgres": "ok" if postgres_ok else "error",
    }

    if postgres_ok:
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "dependencies": dependencies},
        )

    return JSONResponse(
        status_code=503,
        content={"status": "unavailable", "dependencies": dependencies},
    )
