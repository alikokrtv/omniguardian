import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AllocationRequest, StockRequest
from app.core.errors import DomainError
from app.models.domain import AllocationOrder, InventoryLevel, OutboxEvent, Product, Warehouse
from app.services.audit import record_audit, snapshot
from app.services.billing import consume_allocation
from app.services.telemetry import record_rejection

logger = logging.getLogger(__name__)


def emit_inventory_change(session: AsyncSession, tenant_id: UUID, sku: str):
    now = datetime.now(UTC)
    session.add(
        OutboxEvent(
            tenant_id=tenant_id,
            sku=sku,
            created_at=now,
            processed_at=None,
            attempts=0,
            available_at=now,
            last_error=None,
        )
    )


async def lock_level(session: AsyncSession, tenant_id: UUID, warehouse: str, sku: str):
    level = await session.scalar(
        select(InventoryLevel)
        .join(Warehouse, Warehouse.id == InventoryLevel.warehouse_id)
        .join(Product, Product.id == InventoryLevel.sku_id)
        .where(
            InventoryLevel.tenant_id == tenant_id,
            Warehouse.tenant_id == tenant_id,
            Warehouse.code == warehouse,
            Product.tenant_id == tenant_id,
            Product.merchant_sku == sku,
        )
        .with_for_update(of=InventoryLevel)
    )
    if level is None:
        raise DomainError("INVENTORY_NOT_FOUND", f"No inventory for {warehouse}/{sku}", 404)
    return level


async def allocate(
    session: AsyncSession,
    tenant_id: UUID,
    request: AllocationRequest,
    actor: str = "system:internal",
) -> AllocationOrder:
    try:
        return await _allocate(session, tenant_id, request, actor)
    except DomainError as exc:
        if exc.code == "OUT_OF_STOCK":
            try:
                await record_rejection(session, tenant_id, request)
            except SQLAlchemyError:
                # A metrics failure must not turn a clean stock rejection into a success.
                logger.exception("rejection_telemetry_failed tenant=%s", tenant_id)
        raise


async def _allocate(
    session: AsyncSession,
    tenant_id: UUID,
    request: AllocationRequest,
    actor: str,
) -> AllocationOrder:
    async with session.begin():
        # Unique constraints serialize concurrent duplicate deliveries, including new keys
        # for the same external order. An aborted reservation leaves no partial order.
        allocation_id = uuid4()
        inserted = await session.scalar(
            insert(AllocationOrder)
            .values(
                id=allocation_id,
                tenant_id=tenant_id,
                order_id=request.order_id,
                channel=request.channel,
                idempotency_key=request.idempotency_key,
                request_hash=request.fingerprint(),
                status="RESERVED",
                line_items=[line.model_dump() for line in request.line_items],
                created_at=func.now(),
            )
            .on_conflict_do_nothing()
            .returning(AllocationOrder.id)
        )
        if inserted is None:
            existing = await session.scalar(
                select(AllocationOrder).where(
                    AllocationOrder.tenant_id == tenant_id,
                    AllocationOrder.idempotency_key == request.idempotency_key,
                )
            )
            if existing is None:
                raise DomainError(
                    "ORDER_ALREADY_EXISTS", "External order already has an allocation"
                )
            if existing.request_hash != request.fingerprint():
                raise DomainError("IDEMPOTENCY_CONFLICT", "Key was used with a different request")
            return existing

        await consume_allocation(session, tenant_id)
        # Every inventory writer uses the same global ordering, preventing cart deadlocks.
        for line in sorted(request.line_items, key=lambda line: (line.warehouse_code, line.sku)):
            active = await session.scalar(
                select(Warehouse.is_active).where(
                    Warehouse.tenant_id == tenant_id,
                    Warehouse.code == line.warehouse_code,
                )
            )
            if not active:
                raise DomainError("WAREHOUSE_UNAVAILABLE", "Warehouse is missing or inactive", 404)
            level = await lock_level(session, tenant_id, line.warehouse_code, line.sku)
            afs = level.stock_on_hand - level.committed_b2b - level.in_flight_reserved
            if afs < line.quantity:
                raise DomainError("OUT_OF_STOCK", f"Insufficient AFS for {line.sku}")
            before = snapshot(level)
            level.in_flight_reserved += line.quantity
            record_audit(
                session,
                tenant_id,
                line.sku,
                line.warehouse_code,
                "RESERVE",
                before,
                level,
                actor,
                request.order_id,
                line.quantity,
            )
            emit_inventory_change(session, tenant_id, line.sku)
        await session.flush()
        return await session.get(AllocationOrder, allocation_id)


async def transition(
    session: AsyncSession,
    tenant_id: UUID,
    allocation_id: UUID,
    target: str,
    actor: str = "system:internal",
) -> AllocationOrder:
    async with session.begin():
        order = await session.scalar(
            select(AllocationOrder)
            .where(
                AllocationOrder.tenant_id == tenant_id,
                AllocationOrder.id == allocation_id,
            )
            .with_for_update()
        )
        if order is None:
            raise DomainError("ALLOCATION_NOT_FOUND", "Allocation does not exist", 404)
        if order.status == target:
            return order
        allowed = {
            "CONFIRMED": {"RESERVED"},
            "CANCELLED": {"RESERVED", "CONFIRMED"},
            "FULFILLED": {"CONFIRMED"},
        }
        if order.status not in allowed.get(target, set()):
            raise DomainError("INVALID_TRANSITION", f"Cannot change {order.status} to {target}")
        for line in sorted(
            order.line_items, key=lambda line: (line["warehouse_code"], line["sku"])
        ):
            level = await lock_level(session, tenant_id, line["warehouse_code"], line["sku"])
            before = snapshot(level)
            if target in {"CANCELLED", "FULFILLED"}:
                level.in_flight_reserved -= line["quantity"]
                if target == "FULFILLED":
                    level.stock_on_hand -= line["quantity"]
                emit_inventory_change(session, tenant_id, line["sku"])
            record_audit(
                session,
                tenant_id,
                line["sku"],
                line["warehouse_code"],
                {"CONFIRMED": "CONFIRM", "CANCELLED": "CANCEL", "FULFILLED": "FULFILL"}[target],
                before,
                level,
                actor,
                order.order_id,
                line["quantity"],
            )
        order.status = target
        await session.flush()
        return order


async def set_stock(
    session: AsyncSession,
    tenant_id: UUID,
    sku: str,
    request: StockRequest,
    actor: str = "system:internal",
):
    async with session.begin():
        warehouse = await session.scalar(
            select(Warehouse).where(
                Warehouse.tenant_id == tenant_id,
                Warehouse.code == request.warehouse_code,
            )
        )
        product = await session.scalar(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.merchant_sku == sku,
            )
        )
        if warehouse is None or product is None:
            raise DomainError("CATALOG_NOT_FOUND", "Create the warehouse and product first", 404)
        await session.execute(
            insert(InventoryLevel)
            .values(
                tenant_id=tenant_id,
                warehouse_id=warehouse.id,
                sku_id=product.id,
                stock_on_hand=0,
                committed_b2b=0,
                in_flight_reserved=0,
            )
            .on_conflict_do_nothing()
        )
        level = await lock_level(session, tenant_id, request.warehouse_code, sku)
        if request.stock_on_hand < request.committed_b2b + level.in_flight_reserved:
            raise DomainError(
                "STOCK_CONFLICT", "Physical stock cannot be below commitments and holds"
            )
        before = snapshot(level)
        if request.committed_b2b < level.managed_b2b:
            raise DomainError(
                "WHOLESALE_LOCKED", "Commitments cannot be below confirmed wholesale POs"
            )
        level.stock_on_hand = request.stock_on_hand
        level.committed_b2b = request.committed_b2b
        if snapshot(level) != before:
            record_audit(
                session,
                tenant_id,
                sku,
                request.warehouse_code,
                "MANUAL_ADJUST",
                before,
                level,
                actor,
            )
            emit_inventory_change(session, tenant_id, sku)


async def inventory(session: AsyncSession, tenant_id: UUID, sku: str) -> dict:
    rows = (
        await session.execute(
            select(InventoryLevel, Warehouse.code, Warehouse.is_active)
            .join(Product, Product.id == InventoryLevel.sku_id)
            .join(Warehouse, Warehouse.id == InventoryLevel.warehouse_id)
            .where(InventoryLevel.tenant_id == tenant_id, Product.merchant_sku == sku)
            .order_by(Warehouse.code)
        )
    ).all()
    if not rows:
        raise DomainError("INVENTORY_NOT_FOUND", "SKU has no inventory", 404)
    return {
        "sku": sku,
        "available_for_sale": sum(level.available_for_sale for level, _, active in rows if active),
        "warehouses": [
            {
                "warehouse_code": code,
                "is_active": active,
                "stock_on_hand": level.stock_on_hand,
                "committed_b2b": level.committed_b2b,
                "in_flight_reserved": level.in_flight_reserved,
                "available_for_sale": level.available_for_sale,
                "managed_b2b": level.managed_b2b,
                "revision": level.revision,
            }
            for level, code, active in rows
        ],
    }
