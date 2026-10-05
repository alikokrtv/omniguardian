from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, text

from app.api.suite_schemas import fingerprint
from app.core.errors import DomainError
from app.models.domain import Product
from app.models.suite import MarketplaceSettlement, SettlementLine


async def reconcile_settlement(session, tenant_id, request):
    async with session.begin():
        # Serialize ingestion for an external report without reserving or changing stock.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {
                "key": f"settlement:{tenant_id}:{request.marketplace}:{request.external_id}",
            },
        )
        existing = await session.scalar(
            select(MarketplaceSettlement).where(
                MarketplaceSettlement.tenant_id == tenant_id,
                MarketplaceSettlement.marketplace == request.marketplace,
                MarketplaceSettlement.external_id == request.external_id,
            )
        )
        if existing:
            if existing.request_hash != fingerprint(request):
                raise DomainError(
                    "SETTLEMENT_CONFLICT", "Settlement ID was already reconciled with other data"
                )
            return existing
        identifier = uuid4()
        lines = []
        payout_total = net_total = Decimal("0")
        # One catalog snapshot for the whole report; captured COGS never changes retrospectively.
        products = (
            await session.scalars(
                select(Product).where(
                    Product.tenant_id == tenant_id,
                    Product.merchant_sku.in_([line.sku for line in request.line_items]),
                )
            )
        ).all()
        catalog = {product.merchant_sku: product for product in products}
        for line in request.line_items:
            product = catalog.get(line.sku)
            if product is None:
                raise DomainError("SKU_NOT_FOUND", f"Unknown settlement SKU: {line.sku}", 404)
            if product.currency != request.currency:
                raise DomainError(
                    "CURRENCY_MISMATCH",
                    "COGS and settlement currency must match; convert explicitly",
                )
            deductions = (
                line.commission_fees + line.fba_fees + line.return_chargebacks + line.other_withheld
            )
            payout = line.gross_sales - deductions - (line.ad_spend if line.ads_withheld else 0)
            net = (
                line.gross_sales
                - deductions
                - line.ad_spend
                - line.shipping_cost
                - product.cost_price * line.quantity
            )
            payout_total += payout
            net_total += net
            lines.append(
                SettlementLine(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    settlement_id=identifier,
                    sku_id=product.id,
                    unit_cogs=product.cost_price,
                    **line.model_dump(exclude={"sku"}),
                    expected_disbursement=payout,
                    net_contribution=net,
                )
            )
        settlement = MarketplaceSettlement(
            id=identifier,
            tenant_id=tenant_id,
            **request.model_dump(exclude={"line_items"}),
            expected_disbursement=payout_total,
            payout_variance=request.actual_disbursement - payout_total,
            net_contribution=net_total,
            request_hash=fingerprint(request),
        )
        session.add(settlement)
        await session.flush()
        session.add_all(lines)
        await session.flush()
        return settlement
