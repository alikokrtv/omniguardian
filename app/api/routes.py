from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.adapters.shopify import ShopifyAdapter
from app.api.schemas import AllocationRequest, ProductRequest, StockRequest, WarehouseRequest
from app.core.auth import Principal, admin, allocator, authenticate
from app.core.db import Session
from app.core.errors import DomainError
from app.models.domain import AllocationOrder, Product, Warehouse
from app.services.allocation import allocate, inventory, set_stock, transition

router = APIRouter(prefix="/api/v1")
Reader = Annotated[Principal, Depends(authenticate)]
Writer = Annotated[Principal, Depends(allocator)]
Admin = Annotated[Principal, Depends(admin)]


def order_response(order: AllocationOrder) -> dict:
    return {
        "id": str(order.id),
        "order_id": order.order_id,
        "channel": order.channel,
        "idempotency_key": order.idempotency_key,
        "status": order.status,
        "line_items": order.line_items,
        "created_at": order.created_at,
    }


@router.post("/allocate")
async def reserve(request: AllocationRequest, principal: Writer):
    async with Session() as session:
        return order_response(await allocate(session, principal.tenant_id, request))


@router.get("/allocations/{allocation_id}")
async def get_allocation(allocation_id: UUID, principal: Reader):
    async with Session() as session:
        order = await session.scalar(
            select(AllocationOrder).where(
                AllocationOrder.id == allocation_id,
                AllocationOrder.tenant_id == principal.tenant_id,
            )
        )
        if order is None:
            raise DomainError("ALLOCATION_NOT_FOUND", "Allocation does not exist", 404)
        return order_response(order)


@router.post("/allocations/{allocation_id}/confirm")
async def confirm(allocation_id: UUID, principal: Writer):
    async with Session() as session:
        return order_response(
            await transition(session, principal.tenant_id, allocation_id, "CONFIRMED")
        )


@router.post("/allocations/{allocation_id}/cancel")
async def cancel(allocation_id: UUID, principal: Writer):
    async with Session() as session:
        return order_response(
            await transition(session, principal.tenant_id, allocation_id, "CANCELLED")
        )


@router.post("/allocations/{allocation_id}/fulfill")
async def fulfill(allocation_id: UUID, principal: Writer):
    async with Session() as session:
        return order_response(
            await transition(session, principal.tenant_id, allocation_id, "FULFILLED")
        )


@router.get("/inventory/{sku:path}")
async def get_inventory(sku: str, principal: Reader):
    # Always authoritative. Redis snapshots are for integrations, never checkout decisions.
    async with Session() as session:
        return await inventory(session, principal.tenant_id, sku)


@router.put("/inventory/{sku:path}")
async def update_inventory(sku: str, request: StockRequest, principal: Admin):
    async with Session() as session:
        await set_stock(session, principal.tenant_id, sku, request)
        return await inventory(session, principal.tenant_id, sku)


@router.post("/warehouses", status_code=201)
async def create_warehouse(request: WarehouseRequest, principal: Admin):
    async with Session.begin() as session:
        identifier = await session.scalar(
            insert(Warehouse)
            .values(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                **request.model_dump(),
                is_active=True,
            )
            .on_conflict_do_nothing()
            .returning(Warehouse.id)
        )
        if identifier is None:
            raise DomainError("ALREADY_EXISTS", "Warehouse code already exists")
        return {"id": str(identifier), **request.model_dump()}


@router.post("/products", status_code=201)
async def create_product(request: ProductRequest, principal: Admin):
    async with Session.begin() as session:
        identifier = await session.scalar(
            insert(Product)
            .values(
                id=uuid4(),
                tenant_id=principal.tenant_id,
                **request.model_dump(),
            )
            .on_conflict_do_nothing()
            .returning(Product.id)
        )
        if identifier is None:
            raise DomainError("ALREADY_EXISTS", "Merchant SKU already exists")
        return {"id": str(identifier), **request.model_dump()}


@router.post("/webhooks/shopify/mock")
async def mock_shopify_webhook(request: AllocationRequest, principal: Writer):
    if request.channel != "shopify":
        raise DomainError("INVALID_CHANNEL", "Shopify mock requires channel=shopify", 422)
    return order_response(
        await ShopifyAdapter(principal.tenant_id).handle_incoming_webhook(request.model_dump())
    )
