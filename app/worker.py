"""Durable outbox polling through ARQ. Redis is transport, never the inventory ledger."""

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select, text, update

from app.adapters.registry import build_adapters
from app.core.config import get_settings
from app.core.db import Session, engine
from app.models.domain import OutboxEvent
from app.services.allocation import inventory

logger = logging.getLogger(__name__)


async def dispatch_outbox(ctx):
    async with Session() as session:
        tenants = (
            await session.scalars(
                select(OutboxEvent.tenant_id)
                .where(
                    OutboxEvent.processed_at.is_(None),
                    OutboxEvent.available_at <= datetime.now(UTC),
                )
                .distinct()
                .limit(1000)
            )
        ).all()
    for tenant_id in tenants:
        await ctx["redis"].enqueue_job(
            "sync_tenant", str(tenant_id), _job_id=f"inventory-sync:{tenant_id}"
        )


async def sync_tenant(ctx, tenant: str):
    tenant_id = UUID(tenant)
    async with Session.begin() as session:
        # Transaction-scoped lock serializes channel pushes for this tenant, including
        # duplicate ARQ execution after worker crashes. It does not lock inventory writers.
        locked = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:tenant, 0))"),
            {"tenant": tenant},
        )
        if not locked:
            return
        events = (
            await session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.tenant_id == tenant_id,
                    OutboxEvent.processed_at.is_(None),
                    OutboxEvent.available_at <= datetime.now(UTC),
                )
                .order_by(OutboxEvent.id)
                .limit(500)
            )
        ).all()
        if not events:
            return
        try:
            for sku in sorted({event.sku for event in events}):
                snapshot = await inventory(session, tenant_id, sku)
                for adapter in build_adapters(tenant_id):
                    if not await adapter.sync_inventory(sku, snapshot["available_for_sale"]):
                        raise RuntimeError("Channel rejected inventory update")
                # Bounded-lifetime, explicitly eventual snapshot. API and allocation never read it.
                await ctx["redis"].set(f"inventory:{tenant}:{sku}", json.dumps(snapshot), ex=30)
        except Exception as exc:
            logger.warning(
                "channel_sync_failed tenant=%s error_type=%s", tenant, type(exc).__name__
            )
            for event in events:
                event.attempts += 1
                event.last_error = type(exc).__name__
                event.available_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 2 ** min(event.attempts, 8))
                )
            # Commit retry metadata. A crash before commit leaves events pending for replay.
            return
        await session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id.in_([event.id for event in events]),
            )
            .values(processed_at=datetime.now(UTC), last_error=None)
        )


async def shutdown(ctx):
    await engine.dispose()


class WorkerSettings:
    functions = [sync_tenant]
    cron_jobs = [cron(dispatch_outbox, second=set(range(0, 60, 2)), run_at_startup=True)]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 60
    keep_result = 0
    health_check_interval = 10
