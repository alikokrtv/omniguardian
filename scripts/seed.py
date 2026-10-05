"""Explicit, repeatable local-demo bootstrap. Never overwrites live stock or API keys."""

import asyncio
import hashlib
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.core.db import Session, engine
from app.models.domain import ApiKey, InventoryLevel, Product, Tenant, Warehouse
from app.services.allocation import emit_inventory_change
from app.services.audit import record_audit

TENANT = UUID("00000000-0000-0000-0000-000000000001")
WAREHOUSE = UUID("00000000-0000-0000-0000-000000000002")
PRODUCT = UUID("00000000-0000-0000-0000-000000000003")


async def seed():
    async with Session.begin() as session:
        await session.execute(
            insert(Tenant).values(id=TENANT, name="Local demo").on_conflict_do_nothing()
        )
        await session.execute(
            insert(ApiKey)
            .values(
                tenant_id=TENANT,
                key_hash=hashlib.sha256(get_settings().demo_api_key.encode()).hexdigest(),
                role="admin",
                is_active=True,
            )
            .on_conflict_do_nothing()
        )
        await session.execute(
            insert(Warehouse)
            .values(
                id=WAREHOUSE,
                tenant_id=TENANT,
                code="EH",
                name="East Hub",
                is_active=True,
            )
            .on_conflict_do_nothing()
        )
        await session.execute(
            insert(Product)
            .values(
                id=PRODUCT,
                tenant_id=TENANT,
                merchant_sku="DEMO-SKU",
                title="Demo product",
                barcode=None,
                cost_price=5,
                list_price=10,
            )
            .on_conflict_do_nothing()
        )
        result = await session.scalar(
            insert(InventoryLevel)
            .values(
                tenant_id=TENANT,
                warehouse_id=WAREHOUSE,
                sku_id=PRODUCT,
                stock_on_hand=10,
                committed_b2b=5,
                in_flight_reserved=0,
            )
            .on_conflict_do_nothing()
            .returning(InventoryLevel.sku_id)
        )
        if result:
            level = await session.get(InventoryLevel, (TENANT, WAREHOUSE, PRODUCT))
            record_audit(
                session,
                TENANT,
                "DEMO-SKU",
                "EH",
                "MANUAL_ADJUST",
                {
                    "stock_on_hand": 0,
                    "committed_b2b": 0,
                    "in_flight_reserved": 0,
                    "available_for_sale": 0,
                },
                level,
                "system:demo-seed",
            )
            emit_inventory_change(session, TENANT, "DEMO-SKU")
    await engine.dispose()
    print("Demo ready: warehouse EH, DEMO-SKU, initial AFS=5. Key comes from DEMO_API_KEY.")


if __name__ == "__main__":
    asyncio.run(seed())
