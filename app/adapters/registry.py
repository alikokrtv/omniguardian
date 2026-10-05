from decimal import Decimal
from uuid import UUID

from app.adapters.base import BaseChannelAdapter
from app.adapters.shopify import ShopifyAdapter

ADAPTERS: dict[str, type[BaseChannelAdapter]] = {"shopify": ShopifyAdapter}


def build_adapters(tenant_id: UUID) -> list[BaseChannelAdapter]:
    # Example: publish floor(90% of AFS). Ratios belong in per-tenant channel settings
    # when real channel credentials and adapters are provisioned.
    return [adapter(tenant_id, Decimal("0.90")) for adapter in ADAPTERS.values()]
