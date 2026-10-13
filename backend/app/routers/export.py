"""Exception report export."""
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import joinedload

from app import audit, models
from app.audit import AuditAction
from app.deps import Role, client_ip, require_role, get_tenant, user_agent
from app.export.excel_export import build_exception_report_xlsx
from app.matching.engine import MATCHED
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/gstins", tags=["export"])


@router.get("/{gstin_id}/reconciliation/{period}/export")
def export_exceptions(
    gstin_id: str,
    period: str,
    request: Request,
    actor: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    gstin = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    client = tenant.get_or_404(models.Client, gstin.client_id, "Client not found")

    run = (
        tenant.query(models.ReconciliationRun)
        .filter(models.ReconciliationRun.client_gstin_id == gstin_id,
                models.ReconciliationRun.period == period,
                models.ReconciliationRun.is_current.is_(True))
        .first()
    )
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
        .filter(models.ReconciliationResult.run_id == run.id,
                models.ReconciliationResult.match_status != MATCHED)
        .all()
    )
    if not results:
        raise HTTPException(status_code=404, detail="No exceptions to export for this period.")

    xlsx_bytes = build_exception_report_xlsx(
        results, f"{client.name} ({gstin.gstin})", period
    )
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in client.name)[:40]
    filename = f"GSTR2B_Exceptions_{safe_name}_{gstin.gstin}_{period}.xlsx"

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.EXPORT_GENERATED, entity_type="ReconciliationRun",
        entity_id=run.id,
        metadata={"gstin": gstin.gstin, "period": period, "rows": len(results)},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()

    return StreamingResponse(
        io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
