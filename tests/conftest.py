import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.domain import (
    ApiKey,
    InventoryLevel,
    Product,
    Tenant,
    Warehouse,
)


@pytest_asyncio.fixture
async def world(monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL integration tests")
    schema = "test_" + uuid4().hex
    engine = create_async_engine(
        url,
        pool_size=50,
        max_overflow=10,
        connect_args={"server_settings": {"search_path": schema}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    from app import worker
    from app.adapters import shopify
    from app.api import reporting, routes
    from app.core import auth
    from app.main import app

    for module in [routes, reporting, auth, shopify, worker]:
        monkeypatch.setattr(module, "Session", sessions)
    tenants = [uuid4(), uuid4()]
    keys = [str(uuid4()), str(uuid4())]
    try:
        # Isolated schemas let append-only triggers remain enabled throughout every test.
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            raw = await connection.get_raw_connection()
            for path in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
                await raw.driver_connection.execute(path.read_text(encoding="utf-8"))
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
        # Only the randomly named schema owned by this fixture is dropped. Production audit
        # rows are never deleted, and tests never disable the immutability triggers.
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
