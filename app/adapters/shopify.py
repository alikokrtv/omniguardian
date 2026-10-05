import logging

from app.adapters.base import BaseChannelAdapter
from app.api.schemas import AllocationRequest
from app.core.db import Session
from app.services.allocation import allocate

logger = logging.getLogger(__name__)


class ShopifyAdapter(BaseChannelAdapter):
    """Mock transport with real normalization/allocation. No calls to Shopify are made."""

    async def sync_inventory(self, sku: str, afs_qty: int) -> bool:
        logger.info(
            "mock_inventory_push tenant=%s channel=shopify sku=%s quantity=%s",
            self.tenant_id,
            sku,
            self.buffered_quantity(afs_qty),
        )
        return True

    async def handle_incoming_webhook(self, payload: dict):
        request = AllocationRequest.model_validate({**payload, "channel": "shopify"})
        async with Session() as session:
            return await allocate(session, self.tenant_id, request)
