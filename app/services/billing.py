from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models.domain import MonthlyUsage, Tenant

PLAN_LIMITS = {"STARTER": 1_000, "GROWTH": 10_000, "ENTERPRISE": None}
PLAN_PRICES = {"STARTER": 199, "GROWTH": 499, "ENTERPRISE": 999}


def billing_cycle(now: datetime) -> tuple[date, date]:
    now = now.astimezone(UTC)
    start = date(now.year, now.month, 1)
    end = date(now.year + (now.month == 12), now.month % 12 + 1, 1)
    return start, end


async def consume_allocation(session: AsyncSession, tenant_id: UUID):
    """Must run inside the reservation transaction, after idempotency resolution."""
    tenant = await session.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update(read=True)
    )
    if tenant is None:
        raise DomainError("TENANT_NOT_FOUND", "Tenant does not exist", 404)
    start, _ = billing_cycle(await session.scalar(select(func.now())))
    limit = PLAN_LIMITS[tenant.tier_plan]
    statement = insert(MonthlyUsage).values(
        tenant_id=tenant_id, cycle_start=start, allocation_count=1
    )
    statement = statement.on_conflict_do_update(
        index_elements=[MonthlyUsage.tenant_id, MonthlyUsage.cycle_start],
        set_={"allocation_count": MonthlyUsage.allocation_count + 1},
        where=MonthlyUsage.allocation_count < limit if limit is not None else None,
    ).returning(MonthlyUsage.allocation_count)
    if await session.scalar(statement) is None:
        raise DomainError(
            "USAGE_LIMIT_EXCEEDED",
            f"{tenant.tier_plan} quota reached. Upgrade via your account owner or wait for "
            "the next UTC month; see /api/v1/billing/usage for plan options.",
            402,
        )


async def usage_summary(session: AsyncSession, tenant_id: UUID) -> dict:
    # A single statement makes tier and counter consistent under concurrent plan changes.
    cycle = func.date_trunc("month", func.timezone("UTC", func.now())).cast(
        MonthlyUsage.cycle_start.type
    )
    row = (
        await session.execute(
            select(Tenant.tier_plan, MonthlyUsage.allocation_count, func.now())
            .outerjoin(
                MonthlyUsage,
                (MonthlyUsage.tenant_id == Tenant.id) & (MonthlyUsage.cycle_start == cycle),
            )
            .where(Tenant.id == tenant_id)
        )
    ).one()
    tier, count, now = row
    start, end = billing_cycle(now)
    count = count or 0
    limit = PLAN_LIMITS[tier]
    return {
        "tier_plan": tier,
        "cycle_start": start,
        "cycle_end": end,
        "timezone": "UTC",
        "allocation_count": count,
        "quota": limit,
        "remaining_quota": max(0, limit - count) if limit is not None else None,
        "quota_exceeded": limit is not None and count >= limit,
        "monthly_price_usd": PLAN_PRICES[tier],
        "usage_percent": min(100, round(count / limit * 100, 2)) if limit else None,
        "wholesale_enabled": tier == "ENTERPRISE",
        "plans": [
            {"tier_plan": name, "quota": PLAN_LIMITS[name], "monthly_price_usd": price}
            for name, price in PLAN_PRICES.items()
        ],
    }
