"""Exercise the running HTTP service: fifty buyers, five units, no B2B stock leakage."""

import argparse
import asyncio
import os
import statistics
import time
from collections import Counter
from uuid import uuid4

import httpx


async def run(base_url: str, api_key: str):
    suffix = uuid4().hex[:12]
    sku, warehouse = f"RACE-{suffix}", f"TEST-{suffix}"
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"X-API-Key": api_key},
        timeout=60,
        limits=httpx.Limits(max_connections=60),
    ) as client:
        for path, payload in [
            ("/api/v1/warehouses", {"code": warehouse, "name": "Concurrency demonstration"}),
            ("/api/v1/products", {"merchant_sku": sku, "title": "Concurrency demonstration"}),
        ]:
            response = await client.post(path, json=payload)
            response.raise_for_status()
        response = await client.put(
            f"/api/v1/inventory/{sku}",
            json={
                "warehouse_code": warehouse,
                "stock_on_hand": 10,
                "committed_b2b": 5,
            },
        )
        response.raise_for_status()
        gate = asyncio.Event()

        async def buy(index):
            await gate.wait()
            start = time.perf_counter()
            response = await client.post(
                "/api/v1/allocate",
                json={
                    "order_id": f"{suffix}-{index}",
                    "channel": "shopify",
                    "idempotency_key": f"{suffix}-{index}",
                    "line_items": [
                        {"sku": sku, "warehouse_code": warehouse, "quantity": 1},
                    ],
                },
            )
            return response, (time.perf_counter() - start) * 1000

        tasks = [asyncio.create_task(buy(index)) for index in range(50)]
        gate.set()
        results = await asyncio.gather(*tasks)
        counts = Counter(response.status_code for response, _ in results)
        failures = [response for response, _ in results if response.status_code != 200]
        assert counts == {200: 5, 409: 45}, [(r.status_code, r.text) for r in failures]
        assert all(r.json()["error"]["code"] == "OUT_OF_STOCK" for r in failures)
        response = await client.get(f"/api/v1/inventory/{sku}")
        response.raise_for_status()
        stock = response.json()["warehouses"][0]
        assert stock["available_for_sale"] == 0
        assert stock["stock_on_hand"] == 10
        assert stock["committed_b2b"] == stock["in_flight_reserved"] == 5
        latencies = sorted(ms for _, ms in results)
        print("PASS: 5 reserved; 45 OUT_OF_STOCK; 5 B2B units protected; AFS=0")
        print(f"Latency ms: median={statistics.median(latencies):.1f}, p95={latencies[47]:.1f}")
        print(f"Inspect retained demonstration inventory: {sku} / {warehouse}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--api-key", default=os.getenv("DEMO_API_KEY", "local-demo-key-change-before-deploying")
    )
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.api_key))
