import hashlib
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader

from app.core.db import Session
from app.core.errors import DomainError
from app.models.domain import ApiKey

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Principal:
    tenant_id: UUID
    role: str


async def authenticate(key: Annotated[str | None, Security(api_key_header)]) -> Principal:
    if not key or len(key) > 512:
        raise DomainError("UNAUTHORIZED", "Valid X-API-Key required", 401)
    async with Session() as session:
        record = await session.get(ApiKey, hashlib.sha256(key.encode()).hexdigest())
        if record is None or not record.is_active:
            raise DomainError("UNAUTHORIZED", "Valid X-API-Key required", 401)
        return Principal(record.tenant_id, record.role)


async def allocator(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
    if principal.role not in {"allocator", "admin"}:
        raise DomainError("FORBIDDEN", "Allocator role required", 403)
    return principal


async def admin(principal: Annotated[Principal, Depends(authenticate)]) -> Principal:
    if principal.role != "admin":
        raise DomainError("FORBIDDEN", "Admin role required", 403)
    return principal
