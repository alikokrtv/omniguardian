import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.models.domain import AuditLog, InventoryLevel, MonthlyUsage, OutboxEvent


def order(index=0, quantity=1, sku="A"):
    return {
        "order_id": f"order-{index}",
        "channel": "shopify",
        "idempotency_key": f"key-{index}",
        "line_items": [{"sku": sku, "warehouse_code": "EH", "quantity": quantity}],
    }


async def test_fifty_concurrent_orders_exactly_five_succeed(world):
    gate = asyncio.Event()

    async def submit(index):
        await gate.wait()
        return await world.client.post("/api/v1/allocate", json=order(index))

    tasks = [asyncio.create_task(submit(i)) for i in range(50)]
    gate.set()
    responses = await asyncio.gather(*tasks)
    assert sum(response.status_code == 200 for response in responses) == 5
    failures = [response for response in responses if response.status_code != 200]
    assert len(failures) == 45
    assert all(
        response.status_code == 409 and response.json()["error"]["code"] == "OUT_OF_STOCK"
        for response in failures
    )
    snapshot = (await world.client.get("/api/v1/inventory/A")).json()
    assert snapshot["available_for_sale"] == 0
    assert snapshot["warehouses"][0]["committed_b2b"] == 5
    assert snapshot["warehouses"][0]["stock_on_hand"] == 10
    assert snapshot["warehouses"][0]["in_flight_reserved"] == 5
    assert (await world.client.get("/api/v1/dashboard/telemetry")).json()[
        "total_over_allocation_prevented"
    ] == 45


async def test_concurrent_idempotent_replays(world):
    responses = await asyncio.gather(
        *[world.client.post("/api/v1/allocate", json=order()) for _ in range(50)]
    )
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["id"] for response in responses}) == 1
    assert (await world.client.get("/api/v1/inventory/A")).json()["available_for_sale"] == 4
    async with world.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == world.tenant)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(MonthlyUsage.allocation_count).where(MonthlyUsage.tenant_id == world.tenant)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.tenant_id == world.tenant,
                )
            )
            == 1
        )


async def test_key_payload_mismatch_and_external_order_dedup(world):
    assert (await world.client.post("/api/v1/allocate", json=order())).status_code == 200
    response = await world.client.post("/api/v1/allocate", json=order(quantity=2))
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    response = await world.client.post(
        "/api/v1/allocate", json={**order(), "idempotency_key": "new"}
    )
    assert response.json()["error"]["code"] == "ORDER_ALREADY_EXISTS"


async def test_cart_rollback_and_retry(world):
    data = order()
    data["line_items"].append({"sku": "B", "warehouse_code": "EH", "quantity": 2})
    response = await world.client.post("/api/v1/allocate", json=data)
    assert response.json()["error"]["code"] == "OUT_OF_STOCK"
    assert (await world.client.get("/api/v1/inventory/A")).json()["available_for_sale"] == 5
    async with world.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.tenant_id == world.tenant,
                )
            )
            == 0
        )
    data["line_items"][1]["quantity"] = 1
    assert (await world.client.post("/api/v1/allocate", json=data)).status_code == 200


async def test_confirmation_fulfillment_and_terminal_replay(world):
    allocation = (await world.client.post("/api/v1/allocate", json=order(quantity=2))).json()
    path = f"/api/v1/allocations/{allocation['id']}"
    assert (await world.client.post(path + "/confirm")).json()["status"] == "CONFIRMED"
    before = (await world.client.get("/api/v1/inventory/A")).json()["warehouses"][0]
    assert before["stock_on_hand"] == 10 and before["in_flight_reserved"] == 2
    for _ in range(2):
        assert (await world.client.post(path + "/fulfill")).json()["status"] == "FULFILLED"
    after = (await world.client.get("/api/v1/inventory/A")).json()["warehouses"][0]
    assert after["stock_on_hand"] == 8 and after["in_flight_reserved"] == 0
    assert after["available_for_sale"] == 3
    assert (await world.client.post(path + "/cancel")).status_code == 409
    replay = await world.client.post("/api/v1/allocate", json=order(quantity=2))
    assert replay.json()["status"] == "FULFILLED"


async def test_concurrent_cancel_releases_once(world):
    allocation = (await world.client.post("/api/v1/allocate", json=order(quantity=5))).json()
    path = f"/api/v1/allocations/{allocation['id']}/cancel"
    responses = await asyncio.gather(*[world.client.post(path) for _ in range(20)])
    assert all(response.status_code == 200 for response in responses)
    assert (await world.client.get("/api/v1/inventory/A")).json()["available_for_sale"] == 5


async def test_tenant_isolation(world):
    allocation = (await world.client.post("/api/v1/allocate", json=order())).json()
    headers = {"X-API-Key": world.other_key}
    assert (
        await world.client.get(f"/api/v1/allocations/{allocation['id']}", headers=headers)
    ).status_code == 404
    assert (
        await world.client.post(f"/api/v1/allocations/{allocation['id']}/cancel", headers=headers)
    ).status_code == 404
    other = await world.client.post("/api/v1/allocate", json=order(), headers=headers)
    assert other.status_code == 200 and other.json()["id"] != allocation["id"]


async def test_stock_cannot_erase_holds_or_b2b(world):
    await world.client.post("/api/v1/allocate", json=order(quantity=5))
    response = await world.client.put(
        "/api/v1/inventory/A",
        json={
            "warehouse_code": "EH",
            "stock_on_hand": 9,
            "committed_b2b": 5,
        },
    )
    assert response.json()["error"]["code"] == "STOCK_CONFLICT"


async def test_database_enforces_nonnegative_afs(world):
    with pytest.raises(IntegrityError):
        async with world.sessions.begin() as session:
            await session.execute(
                update(InventoryLevel)
                .where(
                    InventoryLevel.tenant_id == world.tenant,
                )
                .values(stock_on_hand=0, committed_b2b=1)
            )


async def test_auth_and_role_boundary(world):
    from app.models.domain import ApiKey

    assert (
        await world.client.get("/api/v1/inventory/A", headers={"X-API-Key": "invalid"})
    ).status_code == 401
    async with world.sessions.begin() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.tenant_id == world.tenant).values(role="reader")
        )
    assert (await world.client.get("/api/v1/inventory/A")).status_code == 200
    assert (await world.client.post("/api/v1/allocate", json=order())).status_code == 403


async def test_outbox_failure_and_recovery(world, monkeypatch):
    from app import worker

    class Adapter:
        fail = True
        quantities = []

        async def sync_inventory(self, sku, afs):
            if self.fail:
                raise RuntimeError("temporary transport failure")
            self.quantities.append(afs)
            return True

    class Cache:
        async def set(self, *args, **kwargs):
            pass

    adapter = Adapter()
    monkeypatch.setattr(worker, "build_adapters", lambda tenant: [adapter])
    await world.client.post("/api/v1/allocate", json=order())
    await worker.sync_tenant({"redis": Cache()}, str(world.tenant))
    async with world.sessions.begin() as session:
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.tenant_id == world.tenant)
        )
        assert event.attempts == 1 and event.processed_at is None
        event.available_at = datetime.now(UTC) - timedelta(seconds=1)
    adapter.fail = False
    await worker.sync_tenant({"redis": Cache()}, str(world.tenant))
    assert adapter.quantities == [4]
    async with world.sessions() as session:
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.tenant_id == world.tenant)
        )
        assert event.processed_at is not None
