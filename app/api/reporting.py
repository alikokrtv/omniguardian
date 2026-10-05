from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, tuple_

from app.core.auth import Principal, authenticate
from app.core.db import Session
from app.core.errors import DomainError
from app.models.domain import AllocationRejection, AuditLog, InventoryLevel, Product, Warehouse
from app.services.billing import usage_summary

router = APIRouter(prefix="/api/v1")
Reader = Annotated[Principal, Depends(authenticate)]


@router.get("/billing/usage")
async def billing_usage(principal: Reader):
    async with Session() as session:
        return await usage_summary(session, principal.tenant_id)


@router.get("/audit-trail")
async def audit_trail(
    principal: Reader,
    sku: Annotated[str | None, Query(max_length=128)] = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    before_id: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    for value in (start_date, end_date):
        if value is not None and value.utcoffset() is None:
            raise DomainError("INVALID_DATE_RANGE", "Dates must include a timezone offset", 422)
    if start_date and end_date and start_date >= end_date:
        raise DomainError("INVALID_DATE_RANGE", "start_date must precede end_date", 422)
    async with Session() as session:
        query = select(AuditLog).where(AuditLog.tenant_id == principal.tenant_id)
        if sku is not None:
            query = query.where(AuditLog.sku == sku)
        if start_date:
            query = query.where(AuditLog.timestamp >= start_date)
        if end_date:
            query = query.where(AuditLog.timestamp < end_date)
        if before_id:
            cursor = await session.scalar(
                select(AuditLog).where(
                    AuditLog.tenant_id == principal.tenant_id,
                    AuditLog.id == before_id,
                )
            )
            if cursor is None:
                raise DomainError("INVALID_CURSOR", "Audit cursor is unavailable", 422)
            query = query.where(
                tuple_(AuditLog.timestamp, AuditLog.id) < (cursor.timestamp, cursor.id)
            )
        rows = (
            await session.scalars(
                query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).limit(limit + 1)
            )
        ).all()
        return {
            "items": [
                {
                    "id": str(row.id),
                    "tenant_id": str(row.tenant_id),
                    "sku": row.sku,
                    "warehouse_code": row.warehouse_code,
                    "action": row.action,
                    "quantity_delta": row.quantity_delta,
                    "previous_afs": row.previous_afs,
                    "new_afs": row.new_afs,
                    "reference_order_id": row.reference_order_id,
                    "actor": row.actor,
                    "timestamp": row.timestamp,
                    "details": row.details,
                }
                for row in rows[:limit]
            ],
            "next_cursor": str(rows[limit - 1].id) if len(rows) > limit else None,
        }


@router.get("/dashboard/inventory")
async def inventory_matrix(
    principal: Reader,
    after_sku: Annotated[str | None, Query(max_length=128)] = None,
    search: Annotated[str, Query(max_length=128)] = "",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    # Aggregate active warehouses only, consistent with the authoritative SKU AFS endpoint.
    async with Session() as session:
        query = (
            select(
                Product.merchant_sku.label("sku"),
                Product.title,
                func.sum(InventoryLevel.stock_on_hand).label("stock_on_hand"),
                func.sum(InventoryLevel.committed_b2b).label("committed_b2b"),
                func.sum(InventoryLevel.in_flight_reserved).label("in_flight_reserved"),
                func.sum(InventoryLevel.available_for_sale).label("available_for_sale"),
            )
            .select_from(Product)
            .join(InventoryLevel, InventoryLevel.sku_id == Product.id)
            .join(Warehouse, Warehouse.id == InventoryLevel.warehouse_id)
            .where(Product.tenant_id == principal.tenant_id, Warehouse.is_active.is_(True))
        )
        if search:
            query = query.where(
                Product.merchant_sku.icontains(search, autoescape=True)
                | Product.title.icontains(search, autoescape=True)
            )
        if after_sku is not None:
            query = query.where(Product.merchant_sku > after_sku)
        rows = (
            (
                await session.execute(
                    query.group_by(Product.id).order_by(Product.merchant_sku).limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        items = [dict(row) for row in rows[:limit]]
        for item in items:
            afs = item["available_for_sale"]
            item["status"] = "OOS" if afs == 0 else "LOW_STOCK" if afs <= 5 else "IN_STOCK"
        return {"items": items, "next_cursor": items[-1]["sku"] if len(rows) > limit else None}


@router.get("/dashboard/telemetry")
async def telemetry(principal: Reader):
    async with Session() as session:
        count, saved = (
            await session.execute(
                select(
                    func.count(AllocationRejection.id),
                    func.coalesce(func.sum(AllocationRejection.estimated_penalty_usd), 0),
                ).where(AllocationRejection.tenant_id == principal.tenant_id)
            )
        ).one()
        return {
            "total_over_allocation_prevented": count,
            "estimated_penalties_saved_usd": str(saved),
            "currency": "USD",
            "period": "all_time",
            "methodology": "Distinct external orders rejected for insufficient AFS "
            "since tracking began. "
            "Estimated penalties use the tenant's configured USD assumption at rejection; "
            "they are not verified financial savings.",
        }
