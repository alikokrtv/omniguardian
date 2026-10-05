from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import AuditLog, InventoryLevel


def snapshot(level: InventoryLevel) -> dict:
    return {
        "stock_on_hand": level.stock_on_hand,
        "committed_b2b": level.committed_b2b,
        "in_flight_reserved": level.in_flight_reserved,
        "managed_b2b": level.managed_b2b,
        "available_for_sale": level.stock_on_hand - level.committed_b2b - level.in_flight_reserved,
    }


def record_audit(
    session: AsyncSession,
    tenant_id: UUID,
    sku: str,
    warehouse_code: str,
    action: str,
    before: dict,
    level: InventoryLevel,
    actor: str,
    order_id: str | None = None,
    units: int | None = None,
):
    after = snapshot(level)
    entry = AuditLog(
        tenant_id=tenant_id,
        sku=sku,
        warehouse_code=warehouse_code,
        action=action,
        quantity_delta=after["available_for_sale"] - before["available_for_sale"],
        previous_afs=before["available_for_sale"],
        new_afs=after["available_for_sale"],
        reference_order_id=order_id,
        actor=actor,
        details={"before": before, "after": after, "order_line_quantity": units},
    )
    session.add(entry)
    return entry
