from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.api.suite_schemas import fingerprint
from app.core.errors import DomainError
from app.models.domain import Tenant, Warehouse
from app.models.suite import B2BOrder, PalletTag
from app.services.allocation import emit_inventory_change, lock_level
from app.services.audit import record_audit, snapshot
from app.services.billing import consume_allocation


async def require_enterprise(session, tenant_id):
    tier = await session.scalar(
        select(Tenant.tier_plan).where(Tenant.id == tenant_id).with_for_update(read=True)
    )
    if tier != "ENTERPRISE":
        raise DomainError(
            "ENTERPRISE_REQUIRED", "Upgrade to ENTERPRISE ($999/month) for wholesale", 402
        )


async def create_po(session, tenant_id, request):
    async with session.begin():
        await require_enterprise(session, tenant_id)
        identifier = uuid4()
        inserted = await session.scalar(
            insert(B2BOrder)
            .values(
                id=identifier,
                tenant_id=tenant_id,
                **request.model_dump(exclude={"line_items"}),
                line_items=[line.model_dump() for line in request.line_items],
                request_hash=fingerprint(request),
                status="DRAFT",
            )
            .on_conflict_do_nothing()
            .returning(B2BOrder.id)
        )
        if inserted is None:
            existing = await session.scalar(
                select(B2BOrder).where(
                    B2BOrder.tenant_id == tenant_id,
                    B2BOrder.idempotency_key == request.idempotency_key,
                )
            )
            if existing is None or existing.request_hash != fingerprint(request):
                raise DomainError(
                    "PO_CONFLICT", "PO or idempotency key already exists with other data"
                )
            return existing
        return await session.get(B2BOrder, identifier)


async def transition_po(session, tenant_id, identifier, target, actor):
    async with session.begin():
        order = await session.scalar(
            select(B2BOrder)
            .where(
                B2BOrder.tenant_id == tenant_id,
                B2BOrder.id == identifier,
            )
            .with_for_update()
        )
        if order is None:
            raise DomainError("PO_NOT_FOUND", "Wholesale PO does not exist", 404)
        if order.status == target:
            return order
        allowed = {
            "CONFIRMED": {"DRAFT"},
            "CANCELLED": {"DRAFT", "CONFIRMED"},
            "SHIPPED": {"CONFIRMED"},
        }
        if order.status not in allowed[target]:
            raise DomainError("INVALID_TRANSITION", "Wholesale transition is not allowed")
        if target == "CONFIRMED":
            await require_enterprise(session, tenant_id)
            await consume_allocation(session, tenant_id)
        if target == "CONFIRMED" or order.status == "CONFIRMED":
            for line in sorted(order.line_items, key=lambda x: (x["warehouse_code"], x["sku"])):
                warehouse = await session.scalar(
                    select(Warehouse).where(
                        Warehouse.tenant_id == tenant_id,
                        Warehouse.code == line["warehouse_code"],
                    )
                )
                if warehouse is None or (target == "CONFIRMED" and not warehouse.is_active):
                    raise DomainError(
                        "WAREHOUSE_UNAVAILABLE", "Warehouse is missing or inactive", 404
                    )
                level = await lock_level(session, tenant_id, line["warehouse_code"], line["sku"])
                before = snapshot(level)
                quantity = line["quantity"]
                if target == "CONFIRMED":
                    if before["available_for_sale"] < quantity:
                        raise DomainError("OUT_OF_STOCK", "Insufficient AFS for wholesale PO")
                    await session.execute(
                        insert(PalletTag)
                        .values(
                            id=uuid4(),
                            tenant_id=tenant_id,
                            order_id=order.id,
                            warehouse_id=warehouse.id,
                            pallet_code=line["pallet_code"],
                            bin_code=line["bin_code"],
                            is_active=True,
                        )
                        .on_conflict_do_nothing()
                    )
                    tag = await session.scalar(
                        select(PalletTag).where(
                            PalletTag.tenant_id == tenant_id,
                            PalletTag.warehouse_id == warehouse.id,
                            PalletTag.pallet_code == line["pallet_code"],
                            PalletTag.is_active.is_(True),
                        )
                    )
                    if tag.order_id != order.id:
                        raise DomainError(
                            "PALLET_CONFLICT", "Pallet is already tagged to another PO"
                        )
                    level.committed_b2b += quantity
                    level.managed_b2b += quantity
                else:
                    level.committed_b2b -= quantity
                    level.managed_b2b -= quantity
                    if target == "SHIPPED":
                        level.stock_on_hand -= quantity
                action = {
                    "CONFIRMED": "B2B_CONFIRM",
                    "CANCELLED": "B2B_CANCEL",
                    "SHIPPED": "B2B_FULFILL",
                }[target]
                record_audit(
                    session,
                    tenant_id,
                    line["sku"],
                    line["warehouse_code"],
                    action,
                    before,
                    level,
                    actor,
                    order.po_number,
                    quantity,
                )
                emit_inventory_change(session, tenant_id, line["sku"])
        if target in {"CANCELLED", "SHIPPED"}:
            await session.execute(
                update(PalletTag)
                .where(
                    PalletTag.tenant_id == tenant_id,
                    PalletTag.order_id == order.id,
                )
                .values(is_active=False)
            )
        order.status = target
        await session.flush()
        return order
