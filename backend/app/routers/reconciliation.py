"""
Reconciliation runs and the exception review queue.

The important behaviour here is **carry-forward**. Running a reconciliation
recomputes every result row, so results are disposable derived data. The
exceptions attached to them are not: they hold an associate's resolution
notes and a partner's approval. When a run is repeated, exceptions are
matched to the new results by `match_key` — vendor GSTIN plus normalised
invoice number — and their human work is carried across.

Without this, re-running after a corrected upload would silently erase
every note the firm had written, which is the bug the original MVP had.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import joinedload

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_current_user, get_tenant, require_role, user_agent
from app.matching.engine import MATCHED, MatchTolerances, reconcile
from app.money import ZERO, to_decimal, to_float
from app.notifications import NotificationType, notify
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/gstins", tags=["reconciliation"])


def _tax(entry) -> Decimal:
    """The actual input tax credit on a line, i.e. cgst + sgst + igst.

    Dashboard totals are named "ITC" and must mean input tax credit, not the
    taxable (pre-tax) value of the invoice. A prior version of this summed
    taxable_value here, which understates or overstates ITC by whatever the
    firm's tax rate happens to be — wrong in a way that looks plausible,
    which is the worst kind of wrong for a number a partner relies on.
    """
    return to_decimal(entry.cgst) + to_decimal(entry.sgst) + to_decimal(entry.igst)

# The logic lives in app/services/reconciliation_service.py so the scheduled
# fetch can share it. These names stay importable from here for existing callers.
from app.services.reconciliation_service import (  # noqa: E402,F401
    AT_RISK_STATUSES, FLAGGED_STATUSES, NoReconciliationInputs,
    firm_tolerances as _firm_tolerances, itc_lapse_deadline,
    perform_reconciliation, resolve_inputs as _resolve_inputs,
)


@router.post("/{gstin_id}/reconcile", response_model=schemas.DashboardSummary)
def run_reconciliation(
    gstin_id: str,
    request: Request,
    period: str = Query(...),
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    gstin = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    try:
        perform_reconciliation(
            tenant, gstin, period, actor=actor,
            ip_address=client_ip(request), user_agent=user_agent(request),
        )
    except NoReconciliationInputs as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tenant.commit()
    return _build_summary(tenant, gstin_id, period)


def _current_run(tenant: TenantSession, gstin_id: str, period: str):
    return (
        tenant.query(models.ReconciliationRun)
        .filter(models.ReconciliationRun.client_gstin_id == gstin_id,
                models.ReconciliationRun.period == period,
                models.ReconciliationRun.is_current.is_(True))
        .first()
    )


def _build_summary(tenant: TenantSession, gstin_id: str, period: str) -> schemas.DashboardSummary:
    run = _current_run(tenant, gstin_id, period)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail="No reconciliation has been run for this GSTIN and period yet.",
        )

    results = (
        tenant.query(models.ReconciliationResult)
        .options(joinedload(models.ReconciliationResult.purchase_entry),
                 joinedload(models.ReconciliationResult.gstr2b_entry),
                 joinedload(models.ReconciliationResult.exception))
        .filter(models.ReconciliationResult.run_id == run.id)
        .all()
    )

    counts: dict[str, int] = {}
    total_books = ZERO           # ITC claimed per the purchase register (books)
    total_2b = ZERO              # ITC actually eligible, per GSTR-2B
    total_blocked = ZERO         # in GSTR-2B but marked not eligible (e.g. blocked credit)
    at_risk = ZERO
    open_count = 0
    pending_approval = 0

    for r in results:
        status_val = r.match_status.value if hasattr(r.match_status, "value") else r.match_status
        counts[status_val] = counts.get(status_val, 0) + 1
        if r.purchase_entry:
            total_books += _tax(r.purchase_entry)
        if r.gstr2b_entry:
            if r.gstr2b_entry.itc_available:
                total_2b += _tax(r.gstr2b_entry)
            else:
                total_blocked += _tax(r.gstr2b_entry)

        exc = r.exception
        # An approved resolution takes the item off the at-risk figure:
        # the firm has decided and a partner has signed off.
        approved = exc is not None and exc.status in (
            models.ExceptionStatus.APPROVED, models.ExceptionStatus.AUTO_RESOLVED,
        )
        if status_val in AT_RISK_STATUSES and r.purchase_entry and not approved:
            at_risk += _tax(r.purchase_entry)
        if exc is not None:
            if exc.status == models.ExceptionStatus.OPEN:
                open_count += 1
            elif exc.status == models.ExceptionStatus.PENDING_APPROVAL:
                pending_approval += 1

    matched_count = counts.get("MATCHED", 0)
    purchase_count = sum(1 for r in results if r.purchase_entry_id)
    pct = round(100.0 * matched_count / purchase_count, 1) if purchase_count else 0.0

    return schemas.DashboardSummary(
        period=period,
        client_gstin_id=gstin_id,
        run_id=run.id,
        total_itc_in_books=to_float(total_books),
        total_itc_eligible_2b=to_float(total_2b),
        total_itc_blocked_2b=to_float(total_blocked),
        total_itc_at_risk=to_float(at_risk),
        pct_auto_matched=pct,
        counts_by_status=counts,
        total_entries=len(results),
        exceptions_open=open_count,
        exceptions_pending_approval=pending_approval,
    )


@router.get("/{gstin_id}/reconciliation/{period}/summary",
            response_model=schemas.DashboardSummary)
def get_summary(
    gstin_id: str, period: str,
    _: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    return _build_summary(tenant, gstin_id, period)


@router.get("/{gstin_id}/reconciliation/{period}/exceptions",
            response_model=list[schemas.ReconciliationResultOut])
def get_exceptions(
    gstin_id: str,
    period: str,
    match_status: Optional[str] = Query(None),
    exception_status: Optional[models.ExceptionStatus] = Query(None),
    assigned_to: Optional[str] = Query(None),
    _: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """The review queue: every result except clean matches."""
    tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    run = _current_run(tenant, gstin_id, period)
    if run is None:
        return []

    q = (
        tenant.query(models.ReconciliationResult)
        .options(joinedload(models.ReconciliationResult.purchase_entry),
                 joinedload(models.ReconciliationResult.gstr2b_entry),
                 joinedload(models.ReconciliationResult.exception))
        .filter(models.ReconciliationResult.run_id == run.id,
                models.ReconciliationResult.match_status != MATCHED)
    )
    if match_status:
        q = q.filter(models.ReconciliationResult.match_status == match_status)

    results = q.all()

    if exception_status:
        results = [r for r in results if r.exception and r.exception.status == exception_status]
    if assigned_to:
        results = [r for r in results if r.exception and r.exception.assigned_to == assigned_to]

    return results


@router.get("/{gstin_id}/reconciliation/{period}/results",
            response_model=list[schemas.ReconciliationResultOut])
def get_all_results(
    gstin_id: str, period: str,
    _: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    run = _current_run(tenant, gstin_id, period)
    if run is None:
        return []
    return (
        tenant.query(models.ReconciliationResult)
        .options(joinedload(models.ReconciliationResult.purchase_entry),
                 joinedload(models.ReconciliationResult.gstr2b_entry),
                 joinedload(models.ReconciliationResult.exception))
        .filter(models.ReconciliationResult.run_id == run.id)
        .all()
    )


@router.get("/{gstin_id}/reconciliation/{period}/runs",
            response_model=list[schemas.ReconciliationRunOut])
def list_runs(
    gstin_id: str, period: str,
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    return (
        tenant.query(models.ReconciliationRun)
        .filter(models.ReconciliationRun.client_gstin_id == gstin_id,
                models.ReconciliationRun.period == period)
        .order_by(models.ReconciliationRun.version.desc())
        .all()
    )
