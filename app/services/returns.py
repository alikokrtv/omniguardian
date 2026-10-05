from datetime import UTC, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.api.suite_schemas import fingerprint
from app.core.errors import DomainError
from app.models.domain import InventoryLevel, Product, Warehouse
from app.models.suite import ClaimDossier, ReturnClaim
from app.services.allocation import emit_inventory_change, lock_level
from app.services.audit import record_audit, snapshot


def claim_deadline(initiated_at, timezone, holidays, days=14):
    local = initiated_at.astimezone(ZoneInfo(timezone))
    holidays = set(holidays)
    elapsed = 0
    while elapsed < days:
        local += timedelta(days=1)
        if local.weekday() < 5 and local.date() not in holidays:
            elapsed += 1
    return local.astimezone(UTC)


async def create_return(session, tenant_id, request):
    async with session.begin():
        now = await session.scalar(select(func.now()))
        if request.initiated_at > now:
            raise DomainError(
                "INVALID_RETURN_DATE", "Return initiation cannot be in the future", 422
            )
        product = await session.scalar(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.merchant_sku == request.sku,
            )
        )
        warehouse = await session.scalar(
            select(Warehouse).where(
                Warehouse.tenant_id == tenant_id,
                Warehouse.code == request.warehouse_code,
            )
        )
        if product is None or warehouse is None:
            raise DomainError("CATALOG_NOT_FOUND", "Unknown SKU or warehouse", 404)
        identifier = uuid4()
        inserted = await session.scalar(
            insert(ReturnClaim)
            .values(
                id=identifier,
                tenant_id=tenant_id,
                sku_id=product.id,
                warehouse_id=warehouse.id,
                **request.model_dump(exclude={"sku", "warehouse_code", "holidays"}),
                holidays=[day.isoformat() for day in request.holidays],
                request_hash=fingerprint(request),
                claim_due_at=claim_deadline(
                    request.initiated_at, request.business_timezone, request.holidays
                ),
            )
            .on_conflict_do_nothing()
            .returning(ReturnClaim.id)
        )
        if inserted is None:
            existing = await session.scalar(
                select(ReturnClaim).where(
                    ReturnClaim.tenant_id == tenant_id,
                    ReturnClaim.marketplace == request.marketplace,
                    ReturnClaim.rma == request.rma,
                )
            )
            if existing is None or existing.request_hash != fingerprint(request):
                raise DomainError(
                    "RETURN_CONFLICT", "RMA or tracking number already has another return"
                )
            return existing
        return await session.get(ReturnClaim, identifier)


async def locked_return(session, tenant_id, identifier):
    claim = await session.scalar(
        select(ReturnClaim)
        .where(
            ReturnClaim.tenant_id == tenant_id,
            ReturnClaim.id == identifier,
        )
        .with_for_update()
    )
    if claim is None:
        raise DomainError("RETURN_NOT_FOUND", "Return does not exist", 404)
    return claim


async def receive_return(session, tenant_id, identifier, request, actor):
    async with session.begin():
        claim = await locked_return(session, tenant_id, identifier)
        if claim.received_at:
            if claim.condition != request.condition or claim.bin_code != request.bin_code:
                raise DomainError(
                    "RECEIPT_CONFLICT", "Return was already inspected with other data"
                )
            return claim
        sku = await session.scalar(select(Product.merchant_sku).where(Product.id == claim.sku_id))
        warehouse = await session.scalar(
            select(Warehouse.code).where(Warehouse.id == claim.warehouse_id)
        )
        await session.execute(
            insert(InventoryLevel)
            .values(
                tenant_id=tenant_id,
                warehouse_id=claim.warehouse_id,
                sku_id=claim.sku_id,
                stock_on_hand=0,
                committed_b2b=0,
                in_flight_reserved=0,
            )
            .on_conflict_do_nothing()
        )
        level = await lock_level(session, tenant_id, warehouse, sku)
        before = snapshot(level)
        if request.condition == "SELLABLE":
            level.stock_on_hand += claim.quantity
            emit_inventory_change(session, tenant_id, sku)
        claim.received_at = await session.scalar(select(func.now()))
        claim.condition = request.condition
        claim.bin_code = request.bin_code
        claim.disposition = "RESTOCKED" if request.condition == "SELLABLE" else "QUARANTINED"
        entry = record_audit(
            session,
            tenant_id,
            sku,
            warehouse,
            "RETURN_RECEIVE",
            before,
            level,
            actor,
            claim.rma,
            claim.quantity,
        )
        entry.details = {
            **entry.details,
            "return_id": str(claim.id),
            "condition": claim.condition,
            "bin_code": claim.bin_code,
            "disposition": claim.disposition,
        }
        dossier = await session.scalar(
            select(ClaimDossier).where(ClaimDossier.return_id == claim.id)
        )
        if dossier is not None and dossier.status == "READY":
            dossier.status = "VOID_RECEIVED"
        await session.flush()
        return claim


async def dispose_quarantine(session, tenant_id, identifier, request, actor):
    async with session.begin():
        claim = await locked_return(session, tenant_id, identifier)
        if claim.disposition == request.disposition:
            return claim
        if claim.disposition != "QUARANTINED":
            raise DomainError("INVALID_DISPOSITION", "Return is not in quarantine")
        if request.disposition == "RESTOCKED" and claim.condition == "DEFECTIVE_SCRAP":
            raise DomainError("SCRAP_NOT_SELLABLE", "Defective scrap cannot be released to AFS")
        sku = await session.scalar(select(Product.merchant_sku).where(Product.id == claim.sku_id))
        warehouse = await session.scalar(
            select(Warehouse.code).where(Warehouse.id == claim.warehouse_id)
        )
        level = await lock_level(session, tenant_id, warehouse, sku)
        before = snapshot(level)
        if request.disposition == "RESTOCKED":
            level.stock_on_hand += claim.quantity
            emit_inventory_change(session, tenant_id, sku)
        entry = record_audit(
            session,
            tenant_id,
            sku,
            warehouse,
            "QUARANTINE_RELEASE",
            before,
            level,
            actor,
            claim.rma,
            claim.quantity,
        )
        entry.details = {
            **entry.details,
            "return_id": str(claim.id),
            "bin_code": claim.bin_code,
            "disposition_before": claim.disposition,
            "disposition_after": request.disposition,
        }
        claim.disposition = request.disposition
        await session.flush()
        return claim


async def generate_claims(session, tenant_id=None):
    """Operator endpoint and ARQ cron share this idempotent, bounded claim generator."""
    async with session.begin():
        query = select(ReturnClaim).where(
            ReturnClaim.received_at.is_(None),
            ReturnClaim.claim_due_at <= func.now(),
            ~select(ClaimDossier.id).where(ClaimDossier.return_id == ReturnClaim.id).exists(),
        )
        if tenant_id:
            query = query.where(ReturnClaim.tenant_id == tenant_id)
        claims = (
            await session.scalars(
                query.order_by(ReturnClaim.claim_due_at)
                .limit(200)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for claim in claims:
            sku = await session.scalar(
                select(Product.merchant_sku).where(Product.id == claim.sku_id)
            )
            warehouse = await session.scalar(
                select(Warehouse.code).where(Warehouse.id == claim.warehouse_id)
            )
            session.add(
                ClaimDossier(
                    id=uuid4(),
                    tenant_id=claim.tenant_id,
                    return_id=claim.id,
                    status="READY",
                    carrier_reference=None,
                    payload={
                        "rma": claim.rma,
                        "carrier": claim.carrier,
                        "tracking_number": claim.tracking_number,
                        "marketplace": claim.marketplace,
                        "sku": sku,
                        "quantity": claim.quantity,
                        "destination_warehouse": warehouse,
                        "requested_reimbursement_usd": str(claim.expected_value_usd),
                        "initiated_at": claim.initiated_at.isoformat(),
                        "due_at": claim.claim_due_at.isoformat(),
                        "business_timezone": claim.business_timezone,
                        "excluded_holidays": claim.holidays,
                        "rule": "Not received after 14 business days; internal escalation policy",
                        "evidence": claim.evidence,
                        "review_required": [
                            "Proof of carrier acceptance",
                            "Proof of value",
                            "Carrier-specific eligibility and filing deadline",
                            "Warehouse non-receipt confirmation",
                        ],
                        "automatically_submitted": False,
                    },
                )
            )
        return {"generated": len(claims)}


async def mark_claim_submitted(session, tenant_id, identifier, reference):
    async with session.begin():
        claim = await locked_return(session, tenant_id, identifier)
        dossier = await session.scalar(
            select(ClaimDossier).where(ClaimDossier.return_id == claim.id)
        )
        if dossier is None:
            raise DomainError("DOSSIER_NOT_FOUND", "Generate the dossier first", 404)
        if dossier.status == "SUBMITTED" and dossier.carrier_reference == reference:
            return dossier
        if dossier.status != "READY" or claim.received_at is not None:
            raise DomainError("CLAIM_NOT_ELIGIBLE", "This claim cannot be marked submitted")
        dossier.status = "SUBMITTED"
        dossier.carrier_reference = reference
        return dossier
