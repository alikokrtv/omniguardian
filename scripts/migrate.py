"""Small, checksum-verified forward-only SQL migration runner."""

import asyncio
import hashlib
from pathlib import Path

import asyncpg

from app.core.config import get_settings


async def migrate():
    connection = await asyncpg.connect(
        get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    )
    try:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(782341990)")
            await connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name text PRIMARY KEY, checksum text NOT NULL, "
                "applied_at timestamptz DEFAULT now())"
            )
            for path in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                existing = await connection.fetchval(
                    "SELECT checksum FROM schema_migrations WHERE name=$1", path.name
                )
                if existing:
                    if existing != checksum:
                        raise RuntimeError(f"Applied migration changed: {path.name}")
                    continue
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO schema_migrations(name, checksum) VALUES($1, $2)",
                    path.name,
                    checksum,
                )
                print(f"Applied {path.name}")
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(migrate())
