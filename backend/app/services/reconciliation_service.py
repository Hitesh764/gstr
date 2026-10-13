"""
Running a reconciliation, independent of who or what asked for it.

Extracted from the reconciliation router so the same logic serves two callers:
a person pressing "Run reconciliation" (which has a request and a signed-in
user), and the scheduled GSTR-2B fetch (which has neither). Keeping one
implementation means a scheduled run cannot drift from a manual one.

**Carry-forward.** A run recomputes every result row, so results are
disposable. The exceptions attached to them hold human work: resolution notes
and approvals. They are matched to the new results by `match_key` (vendor GSTIN
plus normalised invoice number) and re-pointed rather than recreated, which
preserves their ids, their comment threads and every approval. Without this,
re-running after a corrected upload would erase what the firm had written.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from app import audit, models
from app.audit import AuditAction
from app.matching.engine import MATCHED, MatchTolerances, reconcile
from app.money import to_float
from app.notifications import NotificationType, notify
from app.services.ingest import current_gstr2b_fetch, current_purchase_upload
from app.tenancy import TenantSession

# Statuses that represent unclaimed or at-risk input tax credit.
AT_RISK_STATUSES = ("SUPPLIER_NOT_FILED", "VALUE_MISMATCH", "CREDIT_NOTE_DISCREPANCY")

# Statuses that always need a human decision.
FLAGGED_STATUSES = (
    "SUPPLIER_NOT_FILED", "VALUE_MISMATCH", "CREDIT_NOTE_DISCREPANCY", "MISSING_IN_BOOKS",
)


class NoReconciliationInputs(ValueError):
    """Neither a purchase register nor a GSTR-2B exists for the period."""


@dataclass
class ReconciliationOutcome:
    run: models.ReconciliationRun
    results: int
    flagged: int
    carried_forward: int
    cleared: int


def firm_tolerances(tenant: TenantSession) -> MatchTolerances:
    """A firm's configured tolerances, falling back to system defaults."""
    firm = tenant.db.get(models.Firm, tenant.firm_id)
    tol = MatchTolerances()
    if firm is None:
        return tol
    if firm.exact_match_value_tolerance is not None:
        tol.exact_value = to_float(firm.exact_match_value_tolerance)
    if firm.formatting_value_tolerance is not None:
        tol.formatting_value = to_float(firm.formatting_value_tolerance)
    if firm.formatting_date_tolerance_days is not None:
        tol.formatting_date_days = firm.formatting_date_tolerance_days
    if firm.auto_resolve_confidence_threshold is not None:
        tol.auto_resolve_threshold = firm.auto_resolve_confidence_threshold
    return tol


def itc_lapse_deadline(period: str) -> Optional[date]:
    """Last date the ITC for an invoice in `period` can be claimed.

    Under section 16(4) the deadline is the earlier of the 30 November
    following the end of that financial year (the due date of the October
    return, filed in November) or the date the annual return is filed.
    Since the annual return filing date is not known in advance, the
    statutory backstop is used here.

    India's financial year runs April to March, so an invoice dated
    March 2026 belongs to FY 2025-26 and lapses on 30 November 2026,
    whereas one dated April 2026 belongs to FY 2026-27 and lapses a full
    year later.
    """
    try:
        year, month = (int(x) for x in period.split("-"))
    except (ValueError, AttributeError):
        return None
    fy_end_year = year if month >= 4 else year - 1   # FY starting April
    return date(fy_end_year + 1, 11, 30)


def resolve_inputs(tenant: TenantSession, gstin_id: str, period: str):
    """The current purchase register upload and GSTR-2B fetch for a period."""
    return (current_purchase_upload(tenant, gstin_id, period),
            current_gstr2b_fetch(tenant, gstin_id, period))


def perform_reconciliation(
    tenant: TenantSession,
    gstin: models.ClientGSTIN,
    period: str,
    *,
    actor: Optional[models.User] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    trigger: str = "MANUAL",
) -> ReconciliationOutcome:
    """Reconcile one GSTIN for one period. The caller commits.

    `actor` is None for system-initiated runs (the scheduled fetch). Those are
    recorded with no user rather than a made-up one, so the audit trail says
    plainly that no person triggered them.
    """
    gstin_id = gstin.id
    upload, fetch = resolve_inputs(tenant, gstin_id, period)
    if upload is None and fetch is None:
        raise NoReconciliationInputs(
            f"No purchase register or GSTR-2B data has been loaded for "
            f"{gstin.gstin}, period {period}."
        )

    purchase_entries = (
        tenant.query(models.PurchaseRegisterEntry)
        .filter(models.PurchaseRegisterEntry.upload_id == upload.id).all()
        if upload else []
    )
    gstr2b_entries = (
        tenant.query(models.GSTR2BEntry)
        .filter(models.GSTR2BEntry.fetch_id == fetch.id).all()
        if fetch else []
    )

    tolerances = firm_tolerances(tenant)
    results = reconcile(purchase_entries, gstr2b_entries, tolerances=tolerances)

    # --- carry forward existing human work -------------------------------
    prior_exceptions = {
        e.match_key: e for e in tenant.query(models.ReconciliationException)
        .filter(models.ReconciliationException.client_gstin_id == gstin_id,
                models.ReconciliationException.period == period)
        .all()
        if e.match_key
    }

    # Supersede the previous run rather than deleting it.
    for prev in (
        tenant.query(models.ReconciliationRun)
        .filter(models.ReconciliationRun.client_gstin_id == gstin_id,
                models.ReconciliationRun.period == period,
                models.ReconciliationRun.is_current.is_(True))
        .all()
    ):
        prev.is_current = False

    version = 1 + (
        tenant.query(models.ReconciliationRun)
        .filter(models.ReconciliationRun.client_gstin_id == gstin_id,
                models.ReconciliationRun.period == period)
        .count()
    )

    run = models.ReconciliationRun(
        client_gstin_id=gstin_id, period=period, version=version, is_current=True,
        upload_id=upload.id if upload else None,
        fetch_id=fetch.id if fetch else None,
        status=models.RunStatus.RUNNING,
        run_by=actor.id if actor else None,
        tolerances_used=tolerances.as_dict(),
    )
    tenant.add(run)
    tenant.flush()

    deadline = itc_lapse_deadline(period)
    carried = 0
    seen_keys: set = set()

    for r in results:
        result = models.ReconciliationResult(
            run_id=run.id,
            purchase_entry_id=r.purchase_entry_id,
            gstr2b_entry_id=r.gstr2b_entry_id,
            match_status=models.MatchStatus(r.match_status),
            confidence_score=r.confidence_score,
            plain_language_note=r.notes,
            match_key=r.match_key,
        )
        tenant.add(result)
        tenant.flush()

        needs_exception = (
            r.match_status in FLAGGED_STATUSES
            or (r.match_status == "FORMATTING_MISMATCH"
                and r.confidence_score < tolerances.auto_resolve_threshold)
        )
        auto_resolved = (
            r.match_status == "FORMATTING_MISMATCH"
            and r.confidence_score >= tolerances.auto_resolve_threshold
        )
        if not (needs_exception or auto_resolved):
            continue

        seen_keys.add(r.match_key)
        prior = prior_exceptions.get(r.match_key)

        if prior is not None:
            # The same real-world invoice is already being worked on. Re-point
            # the existing exception at the new result rather than cloning it.
            prior.reconciliation_result_id = result.id
            prior.itc_lapse_deadline = deadline if r.match_status in AT_RISK_STATUSES else None
            carried += 1
        else:
            tenant.add(models.ReconciliationException(
                reconciliation_result_id=result.id,
                client_gstin_id=gstin_id,
                period=period,
                match_key=r.match_key,
                status=(models.ExceptionStatus.AUTO_RESOLVED if auto_resolved
                        else models.ExceptionStatus.OPEN),
                itc_lapse_deadline=(deadline if r.match_status in AT_RISK_STATUSES else None),
            ))

    # An exception whose invoice no longer appears as a problem has been
    # resolved by the data itself, typically because the supplier finally
    # filed. Remove it so the queue reflects reality; the audit log retains
    # what was done about it.
    cleared = 0
    for key, exc in prior_exceptions.items():
        if key not in seen_keys:
            tenant.db.delete(exc)
            cleared += 1

    run.status = models.RunStatus.COMPLETED
    run.completed_at = datetime.utcnow()

    flagged = sum(1 for r in results if r.match_status != MATCHED)
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id if actor else None,
        action=AuditAction.RECONCILIATION_RUN,
        entity_type="ReconciliationRun", entity_id=run.id,
        metadata={
            "gstin": gstin.gstin, "period": period, "version": version,
            "results": len(results), "exceptions_carried_forward": carried,
            "exceptions_cleared": cleared, "trigger": trigger,
            "tolerances": tolerances.as_dict(),
        },
        ip_address=ip_address, user_agent=user_agent,
    )

    # Tell the associate responsible for this client that the run finished,
    # unless they are the person who pressed the button.
    client = tenant.get(models.Client, gstin.client_id)
    owner_id = client.assigned_associate_id if client else None
    if owner_id and (actor is None or owner_id != actor.id):
        notify(
            tenant.db, user_id=owner_id, firm_id=tenant.firm_id,
            event_type=NotificationType.RECONCILIATION_COMPLETED,
            title=f"Reconciliation finished for {client.name}",
            message=f"Period {period}: {len(results)} lines reconciled, {flagged} need review.",
            link=f"/gstins/{gstin_id}/dashboard/{period}",
            entity_type="ReconciliationRun", entity_id=run.id,
        )

    return ReconciliationOutcome(
        run=run, results=len(results), flagged=flagged,
        carried_forward=carried, cleared=cleared,
    )
