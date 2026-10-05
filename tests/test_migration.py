import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest


async def test_upgrade_backfills_existing_orders_and_isolates_months():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for migration verification")
    schema = "migration_test_" + uuid4().hex
    tenant = uuid4()
    connection = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1))
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    try:
        async with connection.transaction():
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            await connection.execute(f'SET LOCAL search_path TO "{schema}"')
            await connection.execute((migrations / "001_initial.sql").read_text())
            await connection.execute("INSERT INTO tenants(id, name) VALUES($1, 'existing')", tenant)
            for index, timestamp in enumerate(["2026-09-30 23:59:59+00", "2026-10-01 00:00:00+00"]):
                await connection.execute(
                    "INSERT INTO allocation_orders VALUES "
                    "($1, $2, $3, 'shopify', $3, 'hash', 'CANCELLED', '[]', $4::text::timestamptz)",
                    uuid4(),
                    tenant,
                    str(index),
                    timestamp,
                )
            await connection.execute((migrations / "002_saas.sql").read_text())
            rows = await connection.fetch(
                "SELECT cycle_start, allocation_count FROM monthly_usage ORDER BY cycle_start"
            )
            assert [(str(row["cycle_start"]), row["allocation_count"]) for row in rows] == [
                ("2026-09-01", 1),
                ("2026-10-01", 1),
            ]
            assert await connection.fetchval("SELECT tier_plan FROM tenants") == "STARTER"
            assert await connection.fetchval("SELECT count(*) FROM audit_logs") == 0
    finally:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await connection.close()
