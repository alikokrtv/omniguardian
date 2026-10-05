from abc import ABC, abstractmethod
from decimal import ROUND_FLOOR, Decimal
from uuid import UUID

from app.models.domain import AllocationOrder


class BaseChannelAdapter(ABC):
    def __init__(
        self, tenant_id: UUID, safety_ratio: Decimal = Decimal("1"), actor: str = "system:adapter"
    ):
        if not Decimal("0") <= safety_ratio <= Decimal("1"):
            raise ValueError("Safety ratio must be between zero and one")
        self.tenant_id = tenant_id
        self.safety_ratio = safety_ratio
        self.actor = actor

    def buffered_quantity(self, afs_qty: int) -> int:
        return int(
            (Decimal(max(0, afs_qty)) * self.safety_ratio).to_integral_value(rounding=ROUND_FLOOR)
        )

    @abstractmethod
    async def sync_inventory(self, sku: str, afs_qty: int) -> bool:
        """Set an absolute inventory quantity. Implementations must tolerate repeated pushes."""

    @abstractmethod
    async def handle_incoming_webhook(self, payload: dict) -> AllocationOrder:
        """Normalize an already-authenticated delivery and reserve its inventory."""
