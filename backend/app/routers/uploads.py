"""
Purchase register uploads and GSTR-2B ingestion, both versioned.

Re-uploading the same GSTIN and period does not overwrite. It creates a new
version and marks it current, leaving prior versions intact. A firm can
therefore answer "what changed between the file we filed from and the one
the client sent later", which is exactly the question an auditor asks.

Uploads are validated before they are imported. If any row fails, nothing
is written unless the caller explicitly passes `force=true`, in which case
valid rows are imported and rejected ones are recorded against the upload.
"""
import json
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status,
)

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, require_role, get_tenant, user_agent
from app.ingestion.gstr2b import get_sample_gstr2b_doc, parse_gstr2b_json
from app.ingestion.purchase_register import parse_purchase_register_file
from app.services.ingest import (
    next_version as _next_version,
    store_gstr2b as _store_gstr2b,
    supersede_previous as _supersede_previous,
)
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/gstins", tags=["uploads"])

# Upload limits. Enforced before parsing so a hostile file cannot exhaust
# memory inside pandas.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024      # 10 MB
ALLOWED_REGISTER_EXTENSIONS = (".csv", ".xlsx", ".xls")
ALLOWED_GSTR2B_EXTENSIONS = (".json",)


def _check_period(period: str) -> str:
    period = (period or "").strip()
    parts = period.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or not period.replace("-", "").isdigit():
        raise HTTPException(
            status_code=422,
            detail=f"Period '{period}' is not valid. Use YYYY-MM, for example 2026-03.",
        )
    month = int(parts[1])
    if not 1 <= month <= 12:
        raise HTTPException(status_code=422, detail=f"Period month '{parts[1]}' is not 01-12.")
    return period


async def _read_upload(file: UploadFile, allowed_ext: tuple) -> bytes:
    name = (file.filename or "").lower()
    if not name.endswith(allowed_ext):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type. Allowed: {', '.join(allowed_ext)}.",
        )
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(contents) / 1_048_576:.1f} MB, over the "
                   f"{MAX_UPLOAD_BYTES // 1_048_576} MB limit.",
        )
    return contents


# ---------------------------------------------------------------------------
# Purchase register
# ---------------------------------------------------------------------------

@router.post("/{gstin_id}/uploads/purchase-register",
             response_model=schemas.UploadResultOut)
async def upload_purchase_register(
    gstin_id: str,
    request: Request,
    period: str = Query(..., description="YYYY-MM, e.g. 2026-03"),
    force: bool = Query(False, description="Import valid rows even if some rows failed"),
    file: UploadFile = File(...),
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    period = _check_period(period)
    gstin = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    contents = await _read_upload(file, ALLOWED_REGISTER_EXTENSIONS)

    outcome = parse_purchase_register_file(contents, file.filename, period=period)

    # Nothing is imported while errors stand, unless explicitly forced.
    if outcome.errors and not force:
        return schemas.UploadResultOut(
            upload=None, inserted=0, accepted=False,
            errors=[schemas.RowError(**e.as_dict()) for e in outcome.errors],
            warnings=[schemas.RowError(**w.as_dict()) for w in outcome.warnings],
        )

    if not outcome.rows:
        return schemas.UploadResultOut(
            upload=None, inserted=0, accepted=False,
            errors=[schemas.RowError(**e.as_dict()) for e in outcome.errors]
                   or [schemas.RowError(message="No valid rows found in the file.")],
            warnings=[schemas.RowError(**w.as_dict()) for w in outcome.warnings],
        )

    version = _next_version(tenant, models.PurchaseRegisterUpload, gstin_id, period)
    _supersede_previous(tenant, models.PurchaseRegisterUpload, gstin_id, period)

    upload = models.PurchaseRegisterUpload(
        client_gstin_id=gstin_id,
        period=period,
        version=version,
        is_current=True,
        source="MANUAL_UPLOAD",
        original_filename=file.filename,
        status=models.UploadStatus.IMPORTED,
        row_count=len(outcome.rows),
        error_count=len(outcome.errors),
        validation_errors=(outcome.error_dicts() + outcome.warning_dicts()) or None,
        uploaded_by=actor.id,
    )
    tenant.add(upload)
    tenant.flush()

    for row in outcome.rows:
        tenant.add(models.PurchaseRegisterEntry(upload_id=upload.id, **row))

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.UPLOAD_PURCHASE_REGISTER,
        entity_type="PurchaseRegisterUpload", entity_id=upload.id,
        metadata={
            "gstin": gstin.gstin, "period": period, "version": version,
            "rows": len(outcome.rows), "errors": len(outcome.errors),
            "filename": file.filename, "forced": force,
        },
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    tenant.refresh(upload)

    return schemas.UploadResultOut(
        upload=schemas.UploadOut.model_validate(upload),
        inserted=len(outcome.rows),
        accepted=True,
        errors=[schemas.RowError(**e.as_dict()) for e in outcome.errors],
        warnings=[schemas.RowError(**w.as_dict()) for w in outcome.warnings],
    )


@router.get("/{gstin_id}/uploads", response_model=list[schemas.UploadOut])
def list_uploads(
    gstin_id: str,
    period: Optional[str] = Query(None),
    _: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Full version history for this GSTIN, newest first."""
    tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    q = tenant.query(models.PurchaseRegisterUpload).filter(
        models.PurchaseRegisterUpload.client_gstin_id == gstin_id
    )
    if period:
        q = q.filter(models.PurchaseRegisterUpload.period == period)
    return q.order_by(
        models.PurchaseRegisterUpload.period.desc(),
        models.PurchaseRegisterUpload.version.desc(),
    ).all()


@router.get("/{gstin_id}/uploads/{upload_id}/diff")
def diff_upload_versions(
    gstin_id: str,
    upload_id: str,
    against: Optional[str] = Query(None, description="Upload id to compare with; "
                                                     "defaults to the previous version"),
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    """What changed between two versions of a purchase register.

    Invoices are keyed by vendor GSTIN plus normalised invoice number, so a
    re-export that only reformats invoice numbers does not read as a
    wholesale replacement.
    """
    from app.matching.engine import compute_match_key

    tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    current = tenant.get_or_404(models.PurchaseRegisterUpload, upload_id, "Upload not found")

    if against:
        previous = tenant.get_or_404(models.PurchaseRegisterUpload, against, "Upload not found")
    else:
        previous = (
            tenant.query(models.PurchaseRegisterUpload)
            .filter(models.PurchaseRegisterUpload.client_gstin_id == gstin_id,
                    models.PurchaseRegisterUpload.period == current.period,
                    models.PurchaseRegisterUpload.version < current.version)
            .order_by(models.PurchaseRegisterUpload.version.desc())
            .first()
        )

    def index(upload) -> dict:
        if upload is None:
            return {}
        rows = tenant.query(models.PurchaseRegisterEntry).filter(
            models.PurchaseRegisterEntry.upload_id == upload.id
        ).all()
        return {compute_match_key(r.vendor_gstin, r.invoice_number): r for r in rows}

    old_idx, new_idx = index(previous), index(current)

    added, removed, changed = [], [], []
    for key, row in new_idx.items():
        if key not in old_idx:
            added.append({"invoice_number": row.invoice_number,
                          "vendor_gstin": row.vendor_gstin,
                          "taxable_value": float(row.taxable_value)})
    for key, row in old_idx.items():
        if key not in new_idx:
            removed.append({"invoice_number": row.invoice_number,
                            "vendor_gstin": row.vendor_gstin,
                            "taxable_value": float(row.taxable_value)})
        else:
            new_row = new_idx[key]
            if row.taxable_value != new_row.taxable_value:
                changed.append({
                    "invoice_number": new_row.invoice_number,
                    "vendor_gstin": new_row.vendor_gstin,
                    "from": float(row.taxable_value),
                    "to": float(new_row.taxable_value),
                    "delta": float(new_row.taxable_value - row.taxable_value),
                })

    return {
        "current": {"id": current.id, "version": current.version,
                    "uploaded_at": current.uploaded_at, "rows": len(new_idx)},
        "previous": ({"id": previous.id, "version": previous.version,
                      "uploaded_at": previous.uploaded_at, "rows": len(old_idx)}
                     if previous else None),
        "added": added, "removed": removed, "changed": changed,
        "summary": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


# ---------------------------------------------------------------------------
# GSTR-2B
# ---------------------------------------------------------------------------

@router.post("/{gstin_id}/gstr2b/upload", response_model=schemas.FetchResultOut)
async def upload_gstr2b(
    gstin_id: str,
    request: Request,
    period: str = Query(...),
    file: UploadFile = File(...),
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Manual GSTR-2B JSON upload — the fallback when live GSP access is
    not connected for this client."""
    period = _check_period(period)
    gstin = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    contents = await _read_upload(file, ALLOWED_GSTR2B_EXTENSIONS)

    rows, errors, meta = parse_gstr2b_json(contents)
    if not rows:
        return schemas.FetchResultOut(
            fetch=None, inserted=0,
            errors=[schemas.RowError(message=e) for e in errors]
                   or [schemas.RowError(message="No invoices found in the GSTR-2B file.")],
        )

    # A GSTR-2B filed for a different GSTIN would silently reconcile to
    # nothing, so catch the mix-up here rather than in the results.
    if meta.get("gstin") and meta["gstin"].upper() != gstin.gstin.upper():
        raise HTTPException(
            status_code=422,
            detail=f"This GSTR-2B file is for GSTIN {meta['gstin']}, but you are "
                   f"uploading it against {gstin.gstin}.",
        )

    try:
        raw = json.loads(contents.decode("utf-8"))
    except Exception:
        raw = None

    fetch = _store_gstr2b(tenant, gstin_id, period, rows, meta,
                          models.FetchSource.MANUAL, actor.id, raw_payload=raw)

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.FETCH_GSTR2B, entity_type="GSTR2BFetch", entity_id=fetch.id,
        metadata={"gstin": gstin.gstin, "period": period, "source": "MANUAL",
                  "rows": len(rows), "version": fetch.version},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    tenant.refresh(fetch)

    return schemas.FetchResultOut(
        fetch=schemas.FetchOut.model_validate(fetch),
        inserted=len(rows),
        errors=[schemas.RowError(message=e) for e in errors],
    )


@router.post("/{gstin_id}/gstr2b/sample", response_model=schemas.FetchResultOut)
def load_sample_gstr2b(
    gstin_id: str,
    request: Request,
    period: str = Query(...),
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Load the bundled sample GSTR-2B dataset.

    Stands in for a live GSP pull while the integration is not connected.
    Marked with source=SAMPLE so demo data is never mistaken for real
    filings in an audit trail.
    """
    period = _check_period(period)
    gstin = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")

    doc = get_sample_gstr2b_doc()
    rows, errors, meta = parse_gstr2b_json(json.dumps(doc).encode("utf-8"))
    if not rows:
        raise HTTPException(status_code=500, detail="Sample GSTR-2B data could not be parsed.")

    fetch = _store_gstr2b(tenant, gstin_id, period, rows, meta,
                          models.FetchSource.SAMPLE, actor.id, raw_payload=doc)
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.FETCH_GSTR2B, entity_type="GSTR2BFetch", entity_id=fetch.id,
        metadata={"gstin": gstin.gstin, "period": period, "source": "SAMPLE",
                  "rows": len(rows), "version": fetch.version},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    tenant.refresh(fetch)

    return schemas.FetchResultOut(
        fetch=schemas.FetchOut.model_validate(fetch),
        inserted=len(rows),
        errors=[schemas.RowError(message=e) for e in errors],
    )


@router.get("/{gstin_id}/gstr2b/fetches", response_model=list[schemas.FetchOut])
def list_fetches(
    gstin_id: str,
    period: Optional[str] = Query(None),
    _: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    q = tenant.query(models.GSTR2BFetch).filter(
        models.GSTR2BFetch.client_gstin_id == gstin_id
    )
    if period:
        q = q.filter(models.GSTR2BFetch.period == period)
    return q.order_by(
        models.GSTR2BFetch.period.desc(), models.GSTR2BFetch.version.desc()
    ).all()
