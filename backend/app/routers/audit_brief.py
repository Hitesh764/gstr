"""
The pre-audit brief: one client's GST compliance health, computed from the
reconciliation and exception history already in this platform. See
app/services/audit_brief.py for exactly what is, and is not, in scope.
"""
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app import models
from app.deps import Role, get_tenant, require_role
from app.services import audit_brief as svc
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/clients", tags=["audit-brief"])

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _money(d) -> str:
    return str(d)


def _point_out(p: svc.PeriodPoint) -> dict:
    match_rate = round(100.0 * p.matched_count / p.invoice_count, 1) if p.invoice_count else None
    return {
        "period": p.period,
        "reconciled": p.reconciled,
        "invoice_count": p.invoice_count,
        "matched_count": p.matched_count,
        "match_rate": match_rate,
        "itc_claimed": _money(p.itc_claimed),
        "itc_eligible": _money(p.itc_eligible),
        "itc_blocked": _money(p.itc_blocked),
        "itc_at_risk": _money(p.itc_at_risk),
    }


@router.get("/{client_id}/audit-brief")
def get_audit_brief(
    client_id: str,
    as_of: Optional[str] = Query(None, description="YYYY-MM; defaults to the latest reconciled period"),
    lookback: int = Query(12, ge=1, le=36, description="How many months to look back"),
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    if as_of is not None and not PERIOD_RE.match(as_of):
        raise HTTPException(422, f"'{as_of}' is not a valid period. Use YYYY-MM, for example 2026-05.")
    client = tenant.get_or_404(models.Client, client_id, "Client not found")

    brief = svc.build_audit_brief(tenant, client, as_of=as_of, lookback=lookback)

    return {
        "client_id": brief.client_id,
        "client_name": brief.client_name,
        "as_of": brief.as_of,
        "lookback_periods": brief.lookback_periods,
        "points": [_point_out(p) for p in brief.points],
        "filing_consistency": round(brief.filing_consistency * 100, 1),
        "reconciled_periods": brief.reconciled_periods,
        "total_periods": brief.total_periods,
        "latest_reconciled_period": brief.latest_reconciled_period,
        "latest_itc_at_risk": _money(brief.latest_itc_at_risk),
        "vendor_concentration": [
            {"vendor_gstin": v.vendor_gstin, "vendor_name": v.vendor_name,
             "itc_claimed": _money(v.itc_claimed), "share": round(v.share * 100, 1)}
            for v in brief.vendor_concentration
        ],
        "recurring_vendors": [
            {"vendor_gstin": v.vendor_gstin, "vendor_name": v.vendor_name,
             "periods_flagged": v.periods_flagged, "statuses": v.statuses}
            for v in brief.recurring_vendors
        ],
        "stale_exceptions": [
            {"exception_id": s.exception_id, "period": s.period, "status": s.status,
             "age_days": s.age_days, "vendor_name": s.vendor_name, "invoice_number": s.invoice_number}
            for s in brief.stale_exceptions
        ],
        "open_exception_count": brief.open_exception_count,
        "red_flags": [{"severity": f.severity, "message": f.message} for f in brief.red_flags],
        "generated_at": brief.generated_at,
        "scope_note": (
            "Built from this firm's own GSTR-2B reconciliation and exception history. "
            "Ratio analysis, related-party transactions and cash-payment flags are not "
            "included: they need a general ledger or trial balance, which this platform "
            "does not yet ingest."
        ),
    }
