"""
The firm-wide reconciliation portfolio: every client, one period, at a glance.

This is the answer to "which of my 80 clients actually need a human this
month" — the whole point of automating reconciliation in the first place.
Per-client, per-period dashboards (`app/routers/reconciliation.py`) already
exist; this module rolls them up across a firm so nobody has to open 80 tabs
to find the 12 that matter.

Built as grouped queries over the whole firm rather than one query per
client, so the endpoint stays fast as a firm's client count grows: at most a
handful of queries regardless of whether there are 5 clients or 500.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func

from app import models
from app.money import ZERO, to_decimal, to_float
from app.services.reconciliation_service import AT_RISK_STATUSES
from app.tenancy import TenantSession

# Ordered worst-first, so the row list can be sorted by how much it needs attention.
STATUS_RANK = {
    "NEEDS_REVIEW": 0,
    "AWAITING_APPROVAL": 1,
    "NEEDS_DATA": 2,
    "NOT_STARTED": 3,
    "CLEAN": 4,
}

UNSETTLED_EXCEPTION_STATUSES = (
    models.ExceptionStatus.OPEN,
    models.ExceptionStatus.IN_PROGRESS,
    models.ExceptionStatus.PENDING_APPROVAL,
    models.ExceptionStatus.REWORK,
)


@dataclass
class PortfolioRow:
    client_id: str
    client_name: str
    client_status: str
    assigned_associate_id: Optional[str]
    assigned_associate_name: Optional[str]
    gstin_id: str
    gstin: str
    state_name: Optional[str]
    has_purchase_register: bool
    has_gstr2b: bool
    run_version: Optional[int]
    run_completed_at: Optional[object]
    exceptions_open: int
    exceptions_pending_approval: int
    exceptions_rework: int
    itc_at_risk: Decimal
    nearest_itc_lapse_deadline: Optional[date]
    status: str


@dataclass
class PortfolioSummary:
    period: str
    total_clients: int
    needs_review: int
    awaiting_approval: int
    needs_data: int
    not_started: int
    clean: int
    total_itc_at_risk: Decimal = ZERO
    rows: list = field(default_factory=list)


def _row_status(row: PortfolioRow) -> str:
    if not row.run_version:
        return "NEEDS_DATA" if (row.has_purchase_register or row.has_gstr2b) else "NOT_STARTED"
    if row.exceptions_open or row.exceptions_rework:
        return "NEEDS_REVIEW"
    if row.exceptions_pending_approval:
        return "AWAITING_APPROVAL"
    return "CLEAN"


def build_portfolio(tenant: TenantSession, period: str) -> PortfolioSummary:
    gstins = (
        tenant.query(models.ClientGSTIN)
        .join(models.Client, models.ClientGSTIN.client_id == models.Client.id)
        .filter(models.Client.deleted_at.is_(None))
        .order_by(models.Client.name.asc(), models.ClientGSTIN.gstin.asc())
        .all()
    )
    if not gstins:
        return PortfolioSummary(period=period, total_clients=0, needs_review=0,
                                awaiting_approval=0, needs_data=0, not_started=0, clean=0)

    gstin_ids = [g.id for g in gstins]
    client_ids = {g.client_id for g in gstins}

    clients = {
        c.id: c for c in tenant.query(models.Client).filter(models.Client.id.in_(client_ids)).all()
    }
    assignee_ids = {c.assigned_associate_id for c in clients.values() if c.assigned_associate_id}
    assignees = (
        {u.id: u.name for u in tenant.query(models.User).filter(models.User.id.in_(assignee_ids)).all()}
        if assignee_ids else {}
    )

    # tenant.query() only accepts a mapped class (it needs to know the model to
    # scope by firm_id), so a column-only select goes through with_entities()
    # after the scoped filter, rather than tenant.query(Model.column).
    has_pr = {
        row.client_gstin_id
        for row in tenant.query(models.PurchaseRegisterUpload)
        .filter(models.PurchaseRegisterUpload.client_gstin_id.in_(gstin_ids),
                models.PurchaseRegisterUpload.period == period,
                models.PurchaseRegisterUpload.is_current.is_(True))
        .with_entities(models.PurchaseRegisterUpload.client_gstin_id)
        .all()
    }
    has_2b = {
        row.client_gstin_id
        for row in tenant.query(models.GSTR2BFetch)
        .filter(models.GSTR2BFetch.client_gstin_id.in_(gstin_ids),
                models.GSTR2BFetch.period == period,
                models.GSTR2BFetch.is_current.is_(True))
        .with_entities(models.GSTR2BFetch.client_gstin_id)
        .all()
    }

    runs = {
        run.client_gstin_id: run
        for run in tenant.query(models.ReconciliationRun)
        .filter(models.ReconciliationRun.client_gstin_id.in_(gstin_ids),
                models.ReconciliationRun.period == period,
                models.ReconciliationRun.is_current.is_(True),
                models.ReconciliationRun.status == models.RunStatus.COMPLETED)
        .all()
    }

    # One grouped query for exception counts by status, instead of one query per client.
    exc_counts: dict[tuple, int] = {}
    for gstin_id, status_val, n in (
        tenant.query(models.ReconciliationException)
        .filter(models.ReconciliationException.client_gstin_id.in_(gstin_ids),
                models.ReconciliationException.period == period)
        .with_entities(models.ReconciliationException.client_gstin_id,
                       models.ReconciliationException.status, func.count())
        .group_by(models.ReconciliationException.client_gstin_id,
                 models.ReconciliationException.status)
        .all()
    ):
        exc_counts[(gstin_id, status_val)] = n

    deadlines = {
        gstin_id: deadline
        for gstin_id, deadline in (
            tenant.query(models.ReconciliationException)
            .filter(models.ReconciliationException.client_gstin_id.in_(gstin_ids),
                    models.ReconciliationException.period == period,
                    models.ReconciliationException.status.in_(UNSETTLED_EXCEPTION_STATUSES),
                    models.ReconciliationException.itc_lapse_deadline.isnot(None))
            .with_entities(models.ReconciliationException.client_gstin_id,
                           func.min(models.ReconciliationException.itc_lapse_deadline))
            .group_by(models.ReconciliationException.client_gstin_id)
            .all()
        )
    }

    # ITC at risk: tax on invoices behind an unsettled, at-risk exception,
    # for the current run only (a superseded run's figures are stale).
    at_risk: dict[str, Decimal] = {}
    at_risk_rows = (
        tenant.query(models.ReconciliationException)
        .join(models.ReconciliationResult,
             models.ReconciliationException.reconciliation_result_id == models.ReconciliationResult.id)
        .join(models.PurchaseRegisterEntry,
             models.ReconciliationResult.purchase_entry_id == models.PurchaseRegisterEntry.id)
        .filter(models.ReconciliationException.client_gstin_id.in_(gstin_ids),
                models.ReconciliationException.period == period,
                models.ReconciliationException.status.in_(UNSETTLED_EXCEPTION_STATUSES),
                models.ReconciliationResult.match_status.in_(AT_RISK_STATUSES))
        .with_entities(
            models.ReconciliationException.client_gstin_id,
            models.PurchaseRegisterEntry.cgst, models.PurchaseRegisterEntry.sgst,
            models.PurchaseRegisterEntry.igst,
        )
        .all()
    )
    for gstin_id, cgst, sgst, igst in at_risk_rows:
        at_risk[gstin_id] = at_risk.get(gstin_id, ZERO) + to_decimal(cgst) + to_decimal(sgst) + to_decimal(igst)

    rows: list[PortfolioRow] = []
    for g in gstins:
        client = clients.get(g.client_id)
        run = runs.get(g.id)
        open_n = exc_counts.get((g.id, models.ExceptionStatus.OPEN), 0) \
            + exc_counts.get((g.id, models.ExceptionStatus.IN_PROGRESS), 0)
        pending_n = exc_counts.get((g.id, models.ExceptionStatus.PENDING_APPROVAL), 0)
        rework_n = exc_counts.get((g.id, models.ExceptionStatus.REWORK), 0)

        row = PortfolioRow(
            client_id=g.client_id,
            client_name=client.name if client else "(unknown client)",
            client_status=client.status.value if client else "UNKNOWN",
            assigned_associate_id=client.assigned_associate_id if client else None,
            assigned_associate_name=assignees.get(client.assigned_associate_id) if client else None,
            gstin_id=g.id, gstin=g.gstin, state_name=g.state_name,
            has_purchase_register=g.id in has_pr, has_gstr2b=g.id in has_2b,
            run_version=run.version if run else None,
            run_completed_at=run.completed_at if run else None,
            exceptions_open=open_n, exceptions_pending_approval=pending_n,
            exceptions_rework=rework_n,
            itc_at_risk=at_risk.get(g.id, ZERO),
            nearest_itc_lapse_deadline=deadlines.get(g.id),
            status="",
        )
        row.status = _row_status(row)
        rows.append(row)

    rows.sort(key=lambda r: (
        STATUS_RANK.get(r.status, 9),
        r.nearest_itc_lapse_deadline or date.max,
        -to_float(r.itc_at_risk),
        r.client_name,
    ))

    counts = {"NEEDS_REVIEW": 0, "AWAITING_APPROVAL": 0, "NEEDS_DATA": 0, "NOT_STARTED": 0, "CLEAN": 0}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1

    return PortfolioSummary(
        period=period, total_clients=len(rows),
        needs_review=counts["NEEDS_REVIEW"], awaiting_approval=counts["AWAITING_APPROVAL"],
        needs_data=counts["NEEDS_DATA"], not_started=counts["NOT_STARTED"], clean=counts["CLEAN"],
        total_itc_at_risk=sum((r.itc_at_risk for r in rows), ZERO),
        rows=rows,
    )


def available_periods(tenant: TenantSession) -> list[str]:
    """Every period this firm has any reconciliation run for, newest first."""
    rows = (
        tenant.query(models.ReconciliationRun)
        .with_entities(models.ReconciliationRun.period)
        .distinct()
        .order_by(models.ReconciliationRun.period.desc())
        .all()
    )
    return [r[0] for r in rows]
