"""
The firm-wide reconciliation portfolio: which clients need attention this period.

This is the "80 clients" view — the whole point of automating reconciliation
being that an associate opens this page instead of 80 client dashboards, and
sees exactly the ones that need a human.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app import models
from app.deps import Role, get_tenant, require_role
from app.services import portfolio as svc
from app.services.gstr2b_sync import previous_period
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("/periods")
def list_periods(
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Every period with at least one reconciliation run, for the period picker.

    Always includes the period GSTR-2B most recently became available for,
    even if nobody has reconciled anything yet — that is itself the point of
    this page.

    Staff only (Associate and above), same as the portfolio itself: a
    client-viewer account is scoped to one client and must never see a
    firm-wide rollup naming every other client.
    """
    periods = svc.available_periods(tenant)
    current = previous_period()
    if current not in periods:
        periods = sorted({current, *periods}, reverse=True)
    return {"periods": periods, "default": periods[0] if periods else current}


def _row_out(r) -> dict:
    return {
        "client_id": r.client_id,
        "client_name": r.client_name,
        "client_status": r.client_status,
        "assigned_associate_id": r.assigned_associate_id,
        "assigned_associate_name": r.assigned_associate_name,
        "gstin_id": r.gstin_id,
        "gstin": r.gstin,
        "state_name": r.state_name,
        "has_purchase_register": r.has_purchase_register,
        "has_gstr2b": r.has_gstr2b,
        "run_version": r.run_version,
        "run_completed_at": r.run_completed_at,
        "exceptions_open": r.exceptions_open,
        "exceptions_pending_approval": r.exceptions_pending_approval,
        "exceptions_rework": r.exceptions_rework,
        "itc_at_risk": str(r.itc_at_risk),
        "nearest_itc_lapse_deadline": r.nearest_itc_lapse_deadline,
        "status": r.status,
    }


@router.get("")
def get_portfolio(
    period: Optional[str] = Query(None, description="YYYY-MM; defaults to the latest GSTR-2B period"),
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    period = period or previous_period()
    summary = svc.build_portfolio(tenant, period)
    return {
        "period": summary.period,
        "total_clients": summary.total_clients,
        "needs_review": summary.needs_review,
        "awaiting_approval": summary.awaiting_approval,
        "needs_data": summary.needs_data,
        "not_started": summary.not_started,
        "clean": summary.clean,
        "total_itc_at_risk": str(summary.total_itc_at_risk),
        "rows": [_row_out(r) for r in summary.rows],
    }
