from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.domain import Base


class B2BOrder(Base):
    __tablename__ = "b2b_orders"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    customer: Mapped[str]
    po_number: Mapped[str]
    idempotency_key: Mapped[str]
    request_hash: Mapped[str]
    ship_window_start: Mapped[date]
    ship_window_end: Mapped[date]
    status: Mapped[str]
    line_items: Mapped[list[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class PalletTag(Base):
    __tablename__ = "pallet_tags"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    order_id: Mapped[UUID]
    warehouse_id: Mapped[UUID]
    pallet_code: Mapped[str]
    bin_code: Mapped[str]
    is_active: Mapped[bool]


class ReturnClaim(Base):
    __tablename__ = "return_claims"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    marketplace: Mapped[str]
    rma: Mapped[str]
    carrier: Mapped[str]
    tracking_number: Mapped[str]
    sku_id: Mapped[UUID]
    warehouse_id: Mapped[UUID]
    quantity: Mapped[int]
    expected_value_usd: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    initiated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    business_timezone: Mapped[str]
    holidays: Mapped[list[str]] = mapped_column(JSONB)
    request_hash: Mapped[str]
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    condition: Mapped[str | None]
    bin_code: Mapped[str | None]
    disposition: Mapped[str | None]
    evidence: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class ClaimDossier(Base):
    __tablename__ = "claim_dossiers"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    return_id: Mapped[UUID]
    status: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSONB)
    carrier_reference: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class MarketplaceSettlement(Base):
    __tablename__ = "marketplace_settlements"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    marketplace: Mapped[str]
    external_id: Mapped[str]
    currency: Mapped[str]
    period_start: Mapped[date]
    period_end: Mapped[date]
    actual_disbursement: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    expected_disbursement: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    payout_variance: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    net_contribution: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    request_hash: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class SettlementLine(Base):
    __tablename__ = "settlement_lines"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    settlement_id: Mapped[UUID]
    sku_id: Mapped[UUID]
    quantity: Mapped[int]
    unit_cogs: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    gross_sales: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    commission_fees: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    fba_fees: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    ad_spend: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    return_chargebacks: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    shipping_cost: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    other_withheld: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    ads_withheld: Mapped[bool]
    expected_disbursement: Mapped[Decimal] = mapped_column(Numeric(28, 2))
    net_contribution: Mapped[Decimal] = mapped_column(Numeric(28, 2))


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    source: Mapped[str]
    idempotency_key: Mapped[str]
    request_hash: Mapped[str]
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class BridgeSyncRequest(Base):
    __tablename__ = "bridge_sync_requests"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    provider: Mapped[str]
    idempotency_key: Mapped[str]
    status: Mapped[str]
    import_batch_id: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
