"""Trusted operator CLI; tenant API keys cannot grant themselves a paid plan."""

import argparse
import asyncio
from decimal import Decimal
from uuid import UUID

from sqlalchemy import update

from app.core.db import Session, engine
from app.models.domain import Tenant
from app.services.billing import PLAN_LIMITS


async def set_plan(tenant_id: UUID, tier: str, penalty: Decimal | None):
    values = {"tier_plan": tier}
    if penalty is not None:
        if not penalty.is_finite() or not Decimal("0") <= penalty <= Decimal("999999999999.99"):
            raise ValueError("Penalty assumption must be a finite, nonnegative USD amount")
        if penalty != penalty.quantize(Decimal("0.01")):
            raise ValueError("Penalty assumption must have at most two decimal places")
        values["penalty_estimate_usd"] = penalty
    try:
        async with Session.begin() as session:
            changed = await session.scalar(
                update(Tenant).where(Tenant.id == tenant_id).values(**values).returning(Tenant.id)
            )
            if changed is None:
                raise ValueError("Tenant does not exist")
        print(f"Tenant {tenant_id}: {tier}. Existing cycle usage is preserved.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tenant_id", type=UUID)
    parser.add_argument("tier", choices=PLAN_LIMITS)
    parser.add_argument("--penalty-estimate-usd", type=Decimal)
    args = parser.parse_args()
    asyncio.run(set_plan(args.tenant_id, args.tier, args.penalty_estimate_usd))
