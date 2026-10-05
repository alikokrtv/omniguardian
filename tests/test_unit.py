from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.adapters.shopify import ShopifyAdapter
from app.api.schemas import AllocationRequest


def payload():
    return {
        "order_id": "order-1",
        "channel": "shopify",
        "idempotency_key": "key-1",
        "line_items": [{"sku": "A", "warehouse_code": "EH", "quantity": 1}],
    }


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5, "2", 1_000_000_001])
def test_invalid_quantities(quantity):
    data = payload()
    data["line_items"][0]["quantity"] = quantity
    with pytest.raises(ValidationError):
        AllocationRequest.model_validate(data)


def test_duplicate_lines_rejected():
    data = payload()
    data["line_items"] *= 2
    with pytest.raises(ValidationError):
        AllocationRequest.model_validate(data)


def test_fingerprint_is_line_order_independent():
    data = payload()
    data["line_items"].append({"sku": "B", "warehouse_code": "EH", "quantity": 2})
    first = AllocationRequest.model_validate(data)
    data["line_items"].reverse()
    data["idempotency_key"] = "another-key"
    assert first.fingerprint() == AllocationRequest.model_validate(data).fingerprint()
    data["line_items"][0]["quantity"] = 3
    assert first.fingerprint() != AllocationRequest.model_validate(data).fingerprint()


@pytest.mark.parametrize("afs, expected", [(0, 0), (1, 0), (5, 4), (10, 9), (-1, 0)])
def test_buffer_rounds_down(afs, expected):
    assert ShopifyAdapter(uuid4(), Decimal("0.9")).buffered_quantity(afs) == expected


def test_unsafe_buffer_rejected():
    with pytest.raises(ValueError):
        ShopifyAdapter(uuid4(), Decimal("1.01"))


def test_tenant_cannot_be_supplied_in_request():
    with pytest.raises(ValidationError):
        AllocationRequest.model_validate({**payload(), "tenant_id": str(uuid4())})
