from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Computed, DateTime, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str]


class ApiKey(Base):
    __tablename__ = "api_keys"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[UUID]
    role: Mapped[str]
    is_active: Mapped[bool]


class Warehouse(Base):
    __tablename__ = "warehouses"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    code: Mapped[str]
    name: Mapped[str]
    is_active: Mapped[bool]


class Product(Base):
    __tablename__ = "products"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    merchant_sku: Mapped[str]
    barcode: Mapped[str | None]
    title: Mapped[str]
    cost_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    list_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))


class InventoryLevel(Base):
    __tablename__ = "inventory_levels"
    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    warehouse_id: Mapped[UUID] = mapped_column(primary_key=True)
    sku_id: Mapped[UUID] = mapped_column(primary_key=True)
    stock_on_hand: Mapped[int]
    committed_b2b: Mapped[int]
    in_flight_reserved: Mapped[int]
    available_for_sale: Mapped[int] = mapped_column(
        Computed("stock_on_hand - committed_b2b - in_flight_reserved", persisted=True)
    )


class AllocationOrder(Base):
    __tablename__ = "allocation_orders"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    order_id: Mapped[str]
    channel: Mapped[str]
    idempotency_key: Mapped[str]
    request_hash: Mapped[str]
    status: Mapped[str]
    line_items: Mapped[list[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    sku: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int]
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
