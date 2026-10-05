from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.suite_schemas import CatalogSnapshot
from app.core.errors import DomainError
from app.models.domain import InventoryLevel, Product, Warehouse
from app.services.allocation import emit_inventory_change, lock_level
from app.services.audit import record_audit, snapshot


async def import_catalog(
    session: AsyncSession,
    tenant_id: UUID,
    catalog: CatalogSnapshot,
    actor: str = "system:import",
) -> dict:
    async with session.begin():
        # Ensure all distinct warehouses exist and are active
        warehouse_codes = {row.warehouse_code for row in catalog.rows}
        warehouses = (
            await session.scalars(
                select(Warehouse).where(
                    Warehouse.tenant_id == tenant_id,
                    Warehouse.code.in_(warehouse_codes),
                    Warehouse.is_active.is_(True),
                )
            )
        ).all()
        wh_map = {wh.code: wh for wh in warehouses}
        for code in warehouse_codes:
            if code not in wh_map:
                raise DomainError(
                    "WAREHOUSE_NOT_FOUND", f"Active warehouse '{code}' not found", 404
                )

        # Upsert products first
        for row in catalog.rows:
            await session.execute(
                insert(Product)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    merchant_sku=row.sku,
                    title=row.title,
                    cost_price=row.cost_price,
                    list_price=row.list_price,
                    currency=row.currency,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "merchant_sku"],
                    set_={
                        "title": row.title,
                        "cost_price": row.cost_price,
                        "list_price": row.list_price,
                        "currency": row.currency,
                    },
                )
            )

        # Ensure inventory levels exist
        for row in catalog.rows:
            product = await session.scalar(
                select(Product).where(
                    Product.tenant_id == tenant_id,
                    Product.merchant_sku == row.sku,
                )
            )
            wh = wh_map[row.warehouse_code]
            await session.execute(
                insert(InventoryLevel)
                .values(
                    tenant_id=tenant_id,
                    warehouse_id=wh.id,
                    sku_id=product.id,
                    stock_on_hand=0,
                    committed_b2b=0,
                    in_flight_reserved=0,
                    managed_b2b=0,
                    revision=0,
                )
                .on_conflict_do_nothing()
            )

        # Lock and update inventory levels sorted by (warehouse, sku) to prevent deadlocks
        sorted_rows = sorted(catalog.rows, key=lambda r: (r.warehouse_code, r.sku))
        updated_count = 0
        for row in sorted_rows:
            level = await lock_level(session, tenant_id, row.warehouse_code, row.sku)
            if row.expected_revision is not None and level.revision != row.expected_revision:
                raise DomainError(
                    "STALE_REVISION",
                    f"SKU {row.sku} in warehouse {row.warehouse_code} has revision {level.revision}, expected {row.expected_revision}",
                    409,
                )
            if row.committed_b2b < level.managed_b2b:
                raise DomainError(
                    "WHOLESALE_LOCKED",
                    f"Committed B2B for {row.sku} cannot be less than confirmed wholesale POs ({level.managed_b2b})",
                )
            if row.stock_on_hand < row.committed_b2b + level.in_flight_reserved:
                raise DomainError(
                    "STOCK_CONFLICT",
                    f"Physical stock ({row.stock_on_hand}) cannot be less than committed ({row.committed_b2b}) + in-flight ({level.in_flight_reserved})",
                )

            before = snapshot(level)
            level.stock_on_hand = row.stock_on_hand
            level.committed_b2b = row.committed_b2b

            if snapshot(level) != before:
                entry = record_audit(
                    session,
                    tenant_id,
                    row.sku,
                    row.warehouse_code,
                    "MANUAL_ADJUST",
                    before,
                    level,
                    actor,
                )
                entry.details = {
                    **entry.details,
                    "source": "CATALOG_IMPORT",
                    "revision": level.revision,
                }
                emit_inventory_change(session, tenant_id, row.sku)
                updated_count += 1

        return {
            "status": "SUCCESS",
            "total_rows": len(catalog.rows),
            "updated_rows": updated_count,
        }
