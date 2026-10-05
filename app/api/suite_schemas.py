import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from app.api.schemas import Identifier, Nonnegative, Quantity, StrictModel

Money = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
Marketplace = Literal["amazon", "walmart", "shopify", "mirakl", "target", "best_buy"]
Provider = Literal["zoho", "odoo", "netsuite", "shopify"]


def fingerprint(model) -> str:
    data = model.model_dump(mode="json") if hasattr(model, "model_dump") else model
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class B2BLine(StrictModel):
    sku: Identifier
    warehouse_code: Identifier
    quantity: Quantity
    pallet_code: Identifier
    bin_code: Identifier


class B2BRequest(StrictModel):
    customer: str = Field(min_length=1, max_length=256)
    po_number: Identifier
    idempotency_key: Identifier
    ship_window_start: date
    ship_window_end: date
    line_items: list[B2BLine] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_lines(self):
        if self.ship_window_end < self.ship_window_start:
            raise ValueError("Invalid ship window")
        pairs = [(line.warehouse_code, line.sku) for line in self.line_items]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Combine duplicate warehouse/SKU lines")
        bins = {}
        for line in self.line_items:
            key = (line.warehouse_code, line.pallet_code)
            if key in bins and bins[key] != line.bin_code:
                raise ValueError("A pallet must have a single bin")
            bins[key] = line.bin_code
        return self


class ReturnRequest(StrictModel):
    marketplace: Marketplace
    rma: Identifier
    carrier: Literal["FEDEX", "UPS", "USPS"]
    tracking_number: Identifier
    sku: Identifier
    warehouse_code: Identifier
    quantity: Quantity
    expected_value_usd: Money
    initiated_at: datetime
    business_timezone: str = "America/New_York"
    holidays: list[date] = Field(default_factory=list, max_length=366)
    evidence: dict[str, str] = Field(default_factory=dict, max_length=20)

    @field_validator("initiated_at")
    @classmethod
    def aware(cls, value):
        if value.utcoffset() is None:
            raise ValueError("initiated_at must include a timezone")
        return value

    @field_validator("business_timezone")
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Unknown IANA timezone") from exc
        return value

    @field_validator("evidence")
    @classmethod
    def bounded_evidence(cls, value):
        if any(len(key) > 128 or len(item) > 2048 for key, item in value.items()):
            raise ValueError("Evidence fields are too long")
        return value


class ReturnReceipt(StrictModel):
    condition: Literal["SELLABLE", "DAMAGED_BOX", "DEFECTIVE_SCRAP"]
    bin_code: Identifier


class QuarantineDisposition(StrictModel):
    disposition: Literal["RESTOCKED", "SCRAPPED"]


class ClaimSubmission(StrictModel):
    carrier_reference: Identifier


class SettlementLineRequest(StrictModel):
    sku: Identifier
    quantity: Quantity
    gross_sales: Money
    commission_fees: Money = Decimal("0")
    fba_fees: Money = Decimal("0")
    ad_spend: Money = Decimal("0")
    return_chargebacks: Money = Decimal("0")
    shipping_cost: Money = Decimal("0")
    other_withheld: Money = Decimal("0")
    ads_withheld: bool = False


class SettlementRequest(StrictModel):
    marketplace: Marketplace
    external_id: Identifier
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    period_start: date
    period_end: date
    actual_disbursement: Decimal = Field(max_digits=20, decimal_places=2)
    line_items: list[SettlementLineRequest] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid_report(self):
        if self.period_end < self.period_start:
            raise ValueError("Invalid settlement period")
        if len({line.sku for line in self.line_items}) != len(self.line_items):
            raise ValueError("Combine duplicate SKU settlement lines")
        return self


class CatalogRow(StrictModel):
    sku: Identifier
    title: str = Field(min_length=1, max_length=256)
    warehouse_code: Identifier
    stock_on_hand: Nonnegative
    committed_b2b: Nonnegative = 0
    cost_price: Money = Decimal("0")
    list_price: Money = Decimal("0")
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    barcode: str | None = Field(default=None, max_length=128)
    expected_revision: Annotated[int, Field(strict=True, ge=0)] | None = None


class CatalogSnapshot(StrictModel):
    rows: list[CatalogRow] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_inventory(self):
        keys = [(row.warehouse_code, row.sku) for row in self.rows]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate warehouse/SKU rows")
        products = {}
        for row in self.rows:
            data = row.model_dump(
                exclude={"warehouse_code", "stock_on_hand", "committed_b2b", "expected_revision"}
            )
            if row.sku in products and products[row.sku] != data:
                raise ValueError("Catalog attributes must agree across warehouse rows for each SKU")
            products[row.sku] = data
        return self


class BridgeSync(StrictModel):
    idempotency_key: Identifier
