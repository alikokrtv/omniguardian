import hashlib
import json
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:/-]+$")]
Quantity = Annotated[int, Field(strict=True, ge=1, le=1_000_000_000)]
Nonnegative = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000)]
Channel = Literal["shopify", "amazon", "mirakl", "b2b", "walmart", "target", "best_buy"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AllocationLine(StrictModel):
    sku: Identifier
    warehouse_code: Identifier
    quantity: Quantity


class AllocationRequest(StrictModel):
    order_id: Identifier
    channel: Channel
    idempotency_key: Identifier
    line_items: list[AllocationLine] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_lines(self):
        pairs = [(line.warehouse_code, line.sku) for line in self.line_items]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Duplicate warehouse/SKU lines must be combined")
        return self

    def fingerprint(self) -> str:
        data = self.model_dump(exclude={"idempotency_key"})
        data["line_items"] = sorted(
            data["line_items"], key=lambda line: (line["warehouse_code"], line["sku"])
        )
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


class WarehouseRequest(StrictModel):
    code: Identifier
    name: str = Field(min_length=1, max_length=256)


class ProductRequest(StrictModel):
    merchant_sku: Identifier
    barcode: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    cost_price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    list_price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class StockRequest(StrictModel):
    warehouse_code: Identifier
    stock_on_hand: Nonnegative
    committed_b2b: Nonnegative
