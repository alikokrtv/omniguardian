from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from sqlalchemy import select

from app.api.suite_schemas import (
    B2BRequest,
    BridgeSync,
    CatalogSnapshot,
    ClaimSubmission,
    QuarantineDisposition,
    ReturnReceipt,
    ReturnRequest,
    SettlementRequest,
)
from app.core.auth import Principal, admin, allocator, authenticate
from app.core.db import Session
from app.core.errors import DomainError
from app.models.suite import B2BOrder
from app.services.catalog_import import import_catalog
from app.services.returns import (
    create_return,
    dispose_quarantine,
    generate_claims,
    mark_claim_submitted,
    receive_return,
)
from app.services.settlements import reconcile_settlement
from app.services.wholesale import create_po, transition_po

router = APIRouter(prefix="/api/v1")
Writer = Annotated[Principal, Depends(allocator)]
AdminUser = Annotated[Principal, Depends(admin)]
Reader = Annotated[Principal, Depends(authenticate)]


# =========================================================================
# PILLAR 2: B2B Wholesale Allocation & Hard-Locking
# =========================================================================
@router.post("/wholesale/po")
async def create_wholesale_po(request: B2BRequest, principal: Writer):
    async with Session() as session:
        order = await create_po(session, principal.tenant_id, request)
        return {
            "id": str(order.id),
            "po_number": order.po_number,
            "customer": order.customer,
            "status": order.status,
            "ship_window_start": order.ship_window_start,
            "ship_window_end": order.ship_window_end,
            "line_items": order.line_items,
        }


@router.post("/wholesale/po/{order_id}/transition")
async def transition_wholesale_po(
    order_id: UUID,
    target: Literal["CONFIRMED", "SHIPPED", "CANCELLED"],
    principal: Writer,
):
    async with Session() as session:
        order = await transition_po(session, principal.tenant_id, order_id, target, principal.actor)
        return {
            "id": str(order.id),
            "po_number": order.po_number,
            "customer": order.customer,
            "status": order.status,
            "line_items": order.line_items,
        }


@router.get("/wholesale/po/{order_id}")
async def get_wholesale_po(order_id: UUID, principal: Reader):
    async with Session() as session:
        order = await session.scalar(
            select(B2BOrder).where(
                B2BOrder.tenant_id == principal.tenant_id,
                B2BOrder.id == order_id,
            )
        )
        if order is None:
            raise DomainError("PO_NOT_FOUND", "Wholesale PO not found", 404)
        return {
            "id": str(order.id),
            "po_number": order.po_number,
            "customer": order.customer,
            "status": order.status,
            "ship_window_start": order.ship_window_start,
            "ship_window_end": order.ship_window_end,
            "line_items": order.line_items,
            "created_at": order.created_at,
        }


# =========================================================================
# PILLAR 3: Returns, Quarantine & Carrier Claims Recon
# =========================================================================
@router.post("/returns")
async def register_return(request: ReturnRequest, principal: Writer):
    async with Session() as session:
        claim = await create_return(session, principal.tenant_id, request)
        return {
            "id": str(claim.id),
            "marketplace": claim.marketplace,
            "rma": claim.rma,
            "carrier": claim.carrier,
            "tracking_number": claim.tracking_number,
            "quantity": claim.quantity,
            "expected_value_usd": str(claim.expected_value_usd),
            "claim_due_at": claim.claim_due_at,
            "condition": claim.condition,
            "disposition": claim.disposition,
        }


@router.post("/returns/{return_id}/receive")
async def receive_return_endpoint(
    return_id: UUID,
    receipt: ReturnReceipt,
    principal: Writer,
):
    async with Session() as session:
        claim = await receive_return(
            session, principal.tenant_id, return_id, receipt, principal.actor
        )
        return {
            "id": str(claim.id),
            "condition": claim.condition,
            "bin_code": claim.bin_code,
            "disposition": claim.disposition,
            "received_at": claim.received_at,
        }


@router.post("/returns/{return_id}/quarantine")
async def release_quarantine_endpoint(
    return_id: UUID,
    disposition: QuarantineDisposition,
    principal: Writer,
):
    async with Session() as session:
        claim = await dispose_quarantine(
            session, principal.tenant_id, return_id, disposition, principal.actor
        )
        return {
            "id": str(claim.id),
            "condition": claim.condition,
            "disposition": claim.disposition,
        }


@router.post("/returns/claims/generate")
async def generate_overdue_claims(principal: AdminUser):
    async with Session() as session:
        return await generate_claims(session, principal.tenant_id)


@router.post("/returns/{return_id}/claim-submission")
async def submit_carrier_claim(
    return_id: UUID,
    submission: ClaimSubmission,
    principal: Writer,
):
    async with Session() as session:
        dossier = await mark_claim_submitted(
            session, principal.tenant_id, return_id, submission.carrier_reference
        )
        return {
            "id": str(dossier.id),
            "return_id": str(dossier.return_id),
            "status": dossier.status,
            "carrier_reference": dossier.carrier_reference,
        }


# =========================================================================
# PILLAR 4: Financial Settlements & Reconciliation
# =========================================================================
@router.post("/settlements/reconcile")
async def reconcile_settlement_report(request: SettlementRequest, principal: Writer):
    async with Session() as session:
        settlement = await reconcile_settlement(session, principal.tenant_id, request)
        return {
            "id": str(settlement.id),
            "marketplace": settlement.marketplace,
            "external_id": settlement.external_id,
            "currency": settlement.currency,
            "actual_disbursement": str(settlement.actual_disbursement),
            "expected_disbursement": str(settlement.expected_disbursement),
            "payout_variance": str(settlement.payout_variance),
            "net_contribution": str(settlement.net_contribution),
        }


# =========================================================================
# DATA INGESTION & ZERO-DOWNTIME MIGRATION
# =========================================================================
@router.post("/import/catalog")
async def bulk_import_catalog(snapshot: CatalogSnapshot, principal: AdminUser):
    async with Session() as session:
        return await import_catalog(session, principal.tenant_id, snapshot, principal.actor)


@router.post("/bridge/{provider}/sync")
async def trigger_bridge_sync(
    provider: Annotated[Literal["zoho", "odoo", "netsuite", "shopify"], Path()],
    sync: BridgeSync,
    principal: AdminUser,
):
    # Idempotent bridge acknowledgement
    return {
        "status": "ACKNOWLEDGED",
        "provider": provider,
        "idempotency_key": sync.idempotency_key,
        "tenant_id": str(principal.tenant_id),
    }
