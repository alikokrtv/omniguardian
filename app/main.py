import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, TimeoutError

from app.api.reporting import router as reporting_router
from app.api.routes import router
from app.api.suite_routes import router as suite_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.errors import DomainError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = Redis.from_url(get_settings().redis_url, socket_timeout=2)
    yield
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(title="OmniGuardian", version="0.2.0", lifespan=lifespan)
app.include_router(router)
app.include_router(reporting_router)
app.include_router(suite_router)
WEB_ROOT = Path(__file__).resolve().parent / "web"
app.mount("/dashboard-assets", StaticFiles(directory=WEB_ROOT), name="dashboard-assets")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "X-API-Key"
    if request.url.path.startswith("/dashboard"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
    return response


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(WEB_ROOT / "dashboard.html", headers={"Cache-Control": "no-store"})


@app.exception_handler(DomainError)
async def domain_error(request: Request, exc: DomainError):
    return JSONResponse(
        status_code=exc.status,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
            }
        },
    )


@app.exception_handler(DBAPIError)
@app.exception_handler(TimeoutError)
async def database_error(request: Request, exc: Exception):
    logger.error("database_request_failed error_type=%s", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "1"},
        content={
            "error": {
                "code": "TEMPORARILY_UNAVAILABLE",
                "message": "Retry with the same idempotency key",
            }
        },
    )


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request):
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1 FROM schema_migrations LIMIT 1"))
    try:
        await request.app.state.redis.ping()
        redis_status = "ok"
    except Exception:
        redis_status = "unavailable"
    # Allocation remains safe and available during Redis downtime; the outbox accumulates.
    return {"status": "ok" if redis_status == "ok" else "degraded", "redis": redis_status}
