from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.api.suite_schemas import (
    B2BLine,
    B2BRequest,
    CatalogRow,
    CatalogSnapshot,
    SettlementLineRequest,
    SettlementRequest,
)
from app.services.returns import claim_deadline


def test_claim_deadline_calculation_excludes_weekends_and_holidays():
    # Friday 2026-10-02 at 10:00 AM NY time
    ny_tz = "America/New_York"
    initiated = datetime(2026, 10, 2, 10, 0, tzinfo=ZoneInfo(ny_tz))
    # Holiday on Monday 2026-10-12 (Columbus Day)
    holidays = [date(2026, 10, 12)]

    # 14 business days should skip weekends and the holiday
    deadline = claim_deadline(initiated, ny_tz, holidays, days=14)
    deadline_ny = deadline.astimezone(ZoneInfo(ny_tz))

    # Expect 14 business days later
    assert deadline.tzinfo == UTC
    assert deadline_ny.date() == date(2026, 10, 23)
    assert deadline_ny.weekday() < 5


def test_b2b_request_validation():
    # Valid PO
    req = B2BRequest(
        customer="Ross Stores",
        po_number="PO-2026-001",
        idempotency_key="key-001",
        ship_window_start=date(2026, 10, 10),
        ship_window_end=date(2026, 10, 20),
        line_items=[
            B2BLine(
                sku="UCCSC02-08",
                warehouse_code="EH",
                quantity=100,
                pallet_code="PLT-101",
                bin_code="BIN-A1",
            )
        ],
    )
    assert req.customer == "Ross Stores"
    assert req.line_items[0].quantity == 100

    # Invalid ship window (end before start)
    with pytest.raises(ValueError, match="Invalid ship window"):
        B2BRequest(
            customer="Ross",
            po_number="PO-002",
            idempotency_key="key-002",
            ship_window_start=date(2026, 10, 20),
            ship_window_end=date(2026, 10, 10),
            line_items=[
                B2BLine(
                    sku="UCCSC02-08",
                    warehouse_code="EH",
                    quantity=50,
                    pallet_code="PLT-1",
                    bin_code="BIN-1",
                )
            ],
        )


def test_settlement_reconciliation_math():
    req = SettlementRequest(
        marketplace="amazon",
        external_id="SETTLE-2026-001",
        currency="USD",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 15),
        actual_disbursement=Decimal("1850.00"),
        line_items=[
            SettlementLineRequest(
                sku="UCCSC02-08",
                quantity=100,
                gross_sales=Decimal("3500.00"),
                commission_fees=Decimal("525.00"),
                fba_fees=Decimal("600.00"),
                ad_spend=Decimal("450.00"),
                return_chargebacks=Decimal("75.00"),
                shipping_cost=Decimal("0.00"),
                ads_withheld=True,
            )
        ],
    )
    line = req.line_items[0]
    deductions = (
        line.commission_fees + line.fba_fees + line.return_chargebacks + line.other_withheld
    )
    payout = line.gross_sales - deductions - (line.ad_spend if line.ads_withheld else Decimal("0"))

    # 3500 - (525 + 600 + 75) - 450 = 3500 - 1200 - 450 = 1850.00
    assert payout == Decimal("1850.00")
    assert req.actual_disbursement - payout == Decimal("0.00")


def test_catalog_snapshot_validation():
    # Valid snapshot
    snap = CatalogSnapshot(
        rows=[
            CatalogRow(
                sku="UCCSC02-08",
                title="Nautica Smart Scale",
                warehouse_code="EH",
                stock_on_hand=446,
                committed_b2b=100,
                cost_price=Decimal("6.89"),
                list_price=Decimal("49.99"),
            )
        ]
    )
    assert len(snap.rows) == 1
    assert snap.rows[0].stock_on_hand == 446

    # Duplicate warehouse / SKU in same snapshot should fail
    with pytest.raises(ValueError, match="Duplicate warehouse/SKU rows"):
        CatalogSnapshot(
            rows=[
                CatalogRow(
                    sku="UCCSC02-08",
                    title="Scale",
                    warehouse_code="EH",
                    stock_on_hand=10,
                ),
                CatalogRow(
                    sku="UCCSC02-08",
                    title="Scale",
                    warehouse_code="EH",
                    stock_on_hand=20,
                ),
            ]
        )
