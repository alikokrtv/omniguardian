import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import DBAPIError

from app.models.domain import AuditLog, MonthlyUsage, Tenant
from app.services.billing import billing_cycle
from tests.test_integration import order


async def usage(world):
    response = await world.client.get("/api/v1/billing/usage")
    assert response.status_code == 200, response.text
    return response.json()


async def set_usage(world, count, tier="STARTER", cycle=None):
    async with world.sessions.begin() as session:
        start, _ = billing_cycle(await session.scalar(select(func.now())))
        await session.execute(
            update(Tenant).where(Tenant.id == world.tenant).values(tier_plan=tier)
        )
        await session.execute(
            insert(MonthlyUsage).values(
                tenant_id=world.tenant,
                cycle_start=cycle or start,
                allocation_count=count,
            )
        )


async def test_audit_lifecycle_actor_and_idempotency(world):
    request = order(quantity=2)
    first = await world.client.post("/api/v1/allocate", json=request)
    assert first.status_code == 200
    path = f"/api/v1/allocations/{first.json()['id']}"
    for _ in range(2):
        await world.client.post("/api/v1/allocate", json=request)
        await world.client.post(path + "/confirm")
    await world.client.post(path + "/fulfill")
    await world.client.post(path + "/fulfill")
    result = (await world.client.get("/api/v1/audit-trail")).json()
    rows = list(reversed(result["items"]))
    assert [row["action"] for row in rows] == ["RESERVE", "CONFIRM", "FULFILL"]
    assert [(row["previous_afs"], row["new_afs"], row["quantity_delta"]) for row in rows] == [
        (5, 3, -2),
        (3, 3, 0),
        (3, 3, 0),
    ]
    assert all(row["reference_order_id"] == "order-0" for row in rows)
    assert all(row["actor"].startswith("api_key:") for row in rows)
    assert world.key not in str(result)
    assert rows[-1]["details"]["before"]["stock_on_hand"] == 10
    assert rows[-1]["details"]["after"]["stock_on_hand"] == 8
    assert (await usage(world))["allocation_count"] == 1


async def test_cancel_and_manual_adjust_are_audited(world):
    result = (await world.client.post("/api/v1/allocate", json=order())).json()
    await world.client.post(f"/api/v1/allocations/{result['id']}/cancel")
    payload = {"warehouse_code": "EH", "stock_on_hand": 15, "committed_b2b": 7}
    for _ in range(2):
        assert (await world.client.put("/api/v1/inventory/A", json=payload)).status_code == 200
    rows = (await world.client.get("/api/v1/audit-trail")).json()["items"]
    assert [row["action"] for row in rows] == ["MANUAL_ADJUST", "CANCEL", "RESERVE"]
    assert rows[0]["quantity_delta"] == 3 and rows[0]["new_afs"] == 8
    assert rows[0]["reference_order_id"] is None
    assert rows[1]["quantity_delta"] == 1
    assert (await usage(world))["allocation_count"] == 1


async def test_failed_cart_rolls_back_audit_and_usage_but_records_rejection(world):
    request = order()
    request["line_items"].append({"sku": "B", "warehouse_code": "EH", "quantity": 2})
    for _ in range(3):
        response = await world.client.post("/api/v1/allocate", json=request)
        assert response.json()["error"]["code"] == "OUT_OF_STOCK"
    assert (await world.client.get("/api/v1/audit-trail")).json()["items"] == []
    assert (await usage(world))["allocation_count"] == 0
    telemetry = (await world.client.get("/api/v1/dashboard/telemetry")).json()
    assert telemetry["total_over_allocation_prevented"] == 1
    assert float(telemetry["estimated_penalties_saved_usd"]) == 25


async def test_audit_write_failure_rolls_back_stock_order_and_meter(world, monkeypatch):
    from app.services import allocation

    def fail(*args, **kwargs):
        # Force a real DB constraint error during the same transaction's flush.
        args[0].add(
            AuditLog(
                tenant_id=world.tenant,
                sku="A",
                warehouse_code="EH",
                action="INVALID",
                quantity_delta=0,
                previous_afs=5,
                new_afs=5,
                actor="test",
                reference_order_id=None,
                details={},
            )
        )

    monkeypatch.setattr(allocation, "record_audit", fail)
    response = await world.client.post("/api/v1/allocate", json=order())
    assert response.status_code == 503
    assert (await usage(world))["allocation_count"] == 0
    assert (await world.client.get("/api/v1/inventory/A")).json()["available_for_sale"] == 5


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_logs SET actor='tampered'",
        "DELETE FROM audit_logs",
        "TRUNCATE audit_logs",
    ],
)
async def test_database_rejects_audit_mutations(world, statement):
    await world.client.post("/api/v1/allocate", json=order())
    with pytest.raises(DBAPIError, match="append-only"):
        async with world.sessions.begin() as session:
            await session.execute(text(statement))
    assert len((await world.client.get("/api/v1/audit-trail")).json()["items"]) == 1


async def test_audit_filters_pagination_dates_and_tenant_isolation(world):
    await world.client.post("/api/v1/allocate", json=order(1, sku="A"))
    await world.client.post("/api/v1/allocate", json=order(2, sku="B"))
    first = (await world.client.get("/api/v1/audit-trail", params={"limit": 1})).json()
    assert first["next_cursor"]
    second = (
        await world.client.get(
            "/api/v1/audit-trail",
            params={
                "limit": 1,
                "before_id": first["next_cursor"],
            },
        )
    ).json()
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert second["next_cursor"] is None
    rows = (await world.client.get("/api/v1/audit-trail", params={"sku": "A"})).json()["items"]
    assert len(rows) == 1 and rows[0]["sku"] == "A"
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert (await world.client.get("/api/v1/audit-trail", params={"start_date": future})).json()[
        "items"
    ] == []
    for params in [
        {"start_date": "2026-01-01T00:00:00"},
        {"start_date": future, "end_date": future},
    ]:
        assert (await world.client.get("/api/v1/audit-trail", params=params)).status_code == 422
    headers = {"X-API-Key": world.other_key}
    assert (await world.client.get("/api/v1/audit-trail", headers=headers)).json()["items"] == []
    assert (
        await world.client.get(
            "/api/v1/audit-trail", params={"before_id": first["next_cursor"]}, headers=headers
        )
    ).status_code == 422
    assert (await world.client.get("/api/v1/billing/usage", headers=headers)).json()[
        "allocation_count"
    ] == 0


@pytest.mark.parametrize("tier, limit", [("STARTER", 1000), ("GROWTH", 10000)])
async def test_last_quota_slot_is_atomic_and_replay_remains_available(world, tier, limit):
    await set_usage(world, limit - 1, tier)
    responses = await asyncio.gather(
        *[world.client.post("/api/v1/allocate", json=order(index)) for index in range(20)]
    )
    assert sum(response.status_code == 200 for response in responses) == 1
    assert sum(response.status_code == 402 for response in responses) == 19
    assert all(
        response.json()["error"]["code"] == "USAGE_LIMIT_EXCEEDED"
        for response in responses
        if response.status_code == 402
    )
    index = next(index for index, response in enumerate(responses) if response.status_code == 200)
    replay = await world.client.post("/api/v1/allocate", json=order(index))
    assert replay.status_code == 200
    result = await usage(world)
    assert result["allocation_count"] == limit and result["remaining_quota"] == 0
    assert result["quota_exceeded"] is True
    assert len((await world.client.get("/api/v1/audit-trail")).json()["items"]) == 1
    await world.client.post(f"/api/v1/allocations/{replay.json()['id']}/cancel")
    assert (await usage(world))["allocation_count"] == limit


async def test_enterprise_meters_without_limit(world):
    await set_usage(world, 1_000_000, "ENTERPRISE")
    assert (await world.client.post("/api/v1/allocate", json=order())).status_code == 200
    data = await usage(world)
    assert data["allocation_count"] == 1_000_001
    assert data["quota"] is data["remaining_quota"] is None
    assert not data["quota_exceeded"]


async def test_previous_month_does_not_consume_current_quota(world):
    start, _ = billing_cycle(datetime.now(UTC))
    previous = (start - timedelta(days=1)).replace(day=1)
    await set_usage(world, 1000, cycle=previous)
    assert (await usage(world))["allocation_count"] == 0
    assert (await world.client.post("/api/v1/allocate", json=order())).status_code == 200
    assert (await usage(world))["allocation_count"] == 1


async def test_mock_webhook_is_audited_and_metered(world):
    assert (
        await world.client.post("/api/v1/webhooks/shopify/mock", json=order())
    ).status_code == 200
    assert (await usage(world))["allocation_count"] == 1
    row = (await world.client.get("/api/v1/audit-trail")).json()["items"][0]
    assert row["actor"].startswith("api_key:")


async def test_stock_failure_does_not_consume_final_quota_slot(world):
    await set_usage(world, 999)
    assert (await world.client.post("/api/v1/allocate", json=order(quantity=6))).status_code == 409
    assert (await usage(world))["allocation_count"] == 999
    assert (await world.client.post("/api/v1/allocate", json=order())).status_code == 200
    assert (await usage(world))["allocation_count"] == 1000


async def test_telemetry_deduplicates_external_order_and_is_tenant_scoped(world):
    for key in ["one", "two"]:
        request = {**order(quantity=6), "idempotency_key": key}
        assert (await world.client.post("/api/v1/allocate", json=request)).status_code == 409
    data = (await world.client.get("/api/v1/dashboard/telemetry")).json()
    assert data["total_over_allocation_prevented"] == 1
    other = (
        await world.client.get(
            "/api/v1/dashboard/telemetry",
            headers={
                "X-API-Key": world.other_key,
            },
        )
    ).json()
    assert other["total_over_allocation_prevented"] == 0


async def test_plan_upgrade_preserves_usage_and_opens_capacity(world):
    await set_usage(world, 1000)
    assert (await world.client.post("/api/v1/allocate", json=order())).status_code == 402
    async with world.sessions.begin() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == world.tenant).values(tier_plan="GROWTH")
        )
    assert (await world.client.post("/api/v1/allocate", json=order())).status_code == 200
    data = await usage(world)
    assert data["allocation_count"] == 1001 and data["remaining_quota"] == 8999


async def test_dashboard_reports_and_auth(world):
    page = await world.client.get("/dashboard")
    assert page.status_code == 200 and "Live inventory matrix" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert (await world.client.get("/dashboard-assets/dashboard.js")).status_code == 200
    data = (await world.client.get("/api/v1/dashboard/inventory", params={"limit": 1})).json()
    assert data["items"][0]["sku"] == "A" and data["items"][0]["status"] == "LOW_STOCK"
    assert data["next_cursor"] == "A"
    next_page = (
        await world.client.get("/api/v1/dashboard/inventory", params={"after_sku": "A"})
    ).json()
    assert next_page["items"][0]["sku"] == "B"
    assert (await world.client.get("/api/v1/dashboard/inventory", params={"search": "%"})).json()[
        "items"
    ] == []
    for path in ["audit-trail", "billing/usage", "dashboard/inventory", "dashboard/telemetry"]:
        response = await world.client.get("/api/v1/" + path, headers={"X-API-Key": "wrong"})
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"


def test_december_billing_cycle_and_timezone_boundary():
    assert tuple(
        day.isoformat()
        for day in billing_cycle(datetime.fromisoformat("2027-01-01T01:00:00+03:00"))
    ) == ("2026-12-01", "2027-01-01")
