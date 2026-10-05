import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, TimeoutError

from app.api.routes import router
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


app = FastAPI(title="OmniGuardian", version="0.1.0", lifespan=lifespan)
app.include_router(router)


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
