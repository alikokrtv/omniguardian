import hashlib
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.domain import (
    AllocationOrder,
    ApiKey,
    InventoryLevel,
    OutboxEvent,
    Product,
    Tenant,
    Warehouse,
)


@pytest_asyncio.fixture
async def world(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL integration tests")
    engine = create_async_engine(url, pool_size=50, max_overflow=10)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    from app import worker
    from app.adapters import shopify
    from app.api import routes
    from app.core import auth
    from app.main import app

    for module in [routes, auth, shopify, worker]:
        monkeypatch.setattr(module, "Session", sessions)
    tenants = [uuid4(), uuid4()]
    keys = [str(uuid4()), str(uuid4())]
    try:
        async with sessions.begin() as session:
            for tenant, key in zip(tenants, keys, strict=True):
                session.add(Tenant(id=tenant, name="isolated-test"))
                await session.flush()
                session.add(
                    ApiKey(
                        tenant_id=tenant,
                        key_hash=hashlib.sha256(key.encode()).hexdigest(),
                        role="admin",
                        is_active=True,
                    )
                )
                warehouse = Warehouse(
                    id=uuid4(), tenant_id=tenant, code="EH", name="Test", is_active=True
                )
                session.add(warehouse)
                for sku, stock, committed in [("A", 10, 5), ("B", 1, 0)]:
                    product = Product(
                        id=uuid4(),
                        tenant_id=tenant,
                        merchant_sku=sku,
                        title=sku,
                        barcode=None,
                        cost_price=0,
                        list_price=0,
                    )
                    session.add(product)
                    await session.flush()
                    session.add(
                        InventoryLevel(
                            tenant_id=tenant,
                            warehouse_id=warehouse.id,
                            sku_id=product.id,
                            stock_on_hand=stock,
                            committed_b2b=committed,
                            in_flight_reserved=0,
                        )
                    )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-API-Key": keys[0]},
        ) as client:
            yield SimpleNamespace(
                client=client,
                sessions=sessions,
                tenant=tenants[0],
                other_tenant=tenants[1],
                key=keys[0],
                other_key=keys[1],
            )
    finally:
        # Delete only fixture-owned tenants and rows, never truncate a shared database.
        async with sessions.begin() as session:
            for model in [OutboxEvent, AllocationOrder, InventoryLevel, Product, Warehouse, ApiKey]:
                await session.execute(delete(model).where(model.tenant_id.in_(tenants)))
            await session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        await engine.dispose()
