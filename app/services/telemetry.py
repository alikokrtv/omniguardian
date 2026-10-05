from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.domain import AllocationRejection, Tenant


async def record_rejection(session, tenant_id, request):
    # The failed cart transaction has already rolled back. Deduplicate by external order,
    # so repeated webhooks or fresh idempotency keys do not inflate the ROI estimate.
    async with session.begin():
        estimate = await session.scalar(
            select(Tenant.penalty_estimate_usd).where(Tenant.id == tenant_id)
        )
        await session.execute(
            insert(AllocationRejection)
            .values(
                tenant_id=tenant_id,
                channel=request.channel,
                order_id=request.order_id,
                estimated_penalty_usd=estimate,
            )
            .on_conflict_do_nothing()
        )
