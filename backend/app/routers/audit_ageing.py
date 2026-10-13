"""Open-item ledger ingestion and reproducible debtor/creditor ageing."""
import hashlib
import logging
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.ingestion.audit_ledger import parse_audit_ledger
from app.services.audit_ageing import build_ageing_data
from app.services.related_party_matching import recheck_engagement
from app.storage import FileNotFoundInStorage, StorageError, get_storage
from app.tenancy import TenantSession

logger = logging.getLogger("gstr2b.audit.ageing")
router = APIRouter(prefix="/api/audit-engagements", tags=["audit-ageing"])
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _editable(engagement):
    if engagement.status not in (models.AuditEngagementStatus.PLANNING,
                                  models.AuditEngagementStatus.FIELDWORK,
                                  models.AuditEngagementStatus.COMPLETION):
        raise HTTPException(409, "Ledger inputs cannot be changed after Reporting begins.")


async def _read_upload(file: UploadFile) -> bytes:
    filename = file.filename or ""
    if not filename.lower().endswith((".csv", ".xlsx")):
        raise HTTPException(415, "Unsupported file type. Upload a .csv or .xlsx file.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(422, "The ledger file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Each ledger file is limited to 10 MB.")
    return data


def _row_errors(errors):
    return [schemas.RowError(row=e.row, field=e.field, message=e.message, severity=e.severity) for e in errors]


@router.post("/{engagement_id}/ledger", response_model=schemas.LedgerUploadResultOut)
async def upload_audit_ledger(engagement_id: str, request: Request,
                              as_of_date: date = Query(...),
                              file: UploadFile = File(...),
                              actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                              tenant: TenantSession = Depends(get_tenant)):
    engagement = tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    _editable(engagement)
    start_year = int(engagement.financial_year[:4])
    if not date(start_year, 4, 1) <= as_of_date <= date(start_year + 1, 3, 31):
        raise HTTPException(422, "as_of_date must fall within the engagement financial year.")
    content = await _read_upload(file)
    parsed = parse_audit_ledger(content, file.filename or "")
    if parsed.errors:
        return schemas.LedgerUploadResultOut(accepted=False, errors=_row_errors(parsed.errors))

    prior_upload = tenant.query(models.LedgerUpload).filter(
        models.LedgerUpload.engagement_id == engagement.id,
    ).order_by(models.LedgerUpload.version.desc()).first()
    version = prior_upload.version + 1 if prior_upload else 1
    if prior_upload and prior_upload.is_current:
        prior_upload.is_current = False
    storage = get_storage()
    key = storage.save(firm_id=tenant.firm_id, category="audit_ledger", data=content)
    committed = False
    try:
        upload = models.LedgerUpload(
            engagement_id=engagement.id, version=version, is_current=True,
            original_filename=(file.filename or "ledger.csv")[:400], file_path=key,
            file_sha256=hashlib.sha256(content).hexdigest(), row_count=len(parsed.rows),
            uploaded_by=actor.id,
        )
        tenant.add(upload)
        tenant.flush()
        for row in parsed.rows:
            row["account_type"] = models.AgeingType(row["account_type"])
            tenant.add(models.LedgerLine(upload_id=upload.id, **row))
        tenant.flush()
        lines = tenant.query(models.LedgerLine).filter_by(upload_id=upload.id).all()
        recheck_engagement(tenant, engagement, actor)
        schedules = []
        flags_generated = 0
        for schedule_type in (models.AgeingType.DEBTORS, models.AgeingType.CREDITORS):
            data = build_ageing_data(lines, as_of_date=as_of_date, account_type=schedule_type.value)
            schedule = models.AgeingSchedule(
                engagement_id=engagement.id, schedule_type=schedule_type,
                as_of_date=as_of_date, source_upload_id=upload.id,
                source_version=upload.version, data=data, generated_by=actor.id,
            )
            tenant.add(schedule)
            tenant.flush()
            schedules.append(schedule)
            audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                action=AuditAction.AUDIT_AGEING_SCHEDULE_GENERATED,
                entity_type="AgeingSchedule", entity_id=schedule.id,
                metadata={"engagement_id": engagement.id, "schedule_type": schedule_type.value,
                          "source_upload_id": upload.id, "source_version": upload.version,
                          "as_of_date": as_of_date.isoformat(), "data": data},
                ip_address=client_ip(request), user_agent=user_agent(request))
            for item in data["oldest_bucket_reviews"]:
                note = (f"SYSTEM FLAG — {item['party_name']} invoice {item['invoice_number']} is in the 180+ day bucket; "
                        f"{'aged but explained' if item['explanation_on_file'] else 'aged, no explanation on file'}. "
                        "Verify source documents and evaluate; this is not an audit conclusion.")
                flag = models.RiskFlag(
                    engagement_id=engagement.id, trial_balance_line_id=None, policy_version=None,
                    flag_type=models.RiskFlagType.OTHER, severity="REVIEW_REQUIRED",
                    system_note=note, trigger_data={
                        "subtype": "AGEING_180_PLUS", "schedule_id": schedule.id,
                        "ledger_line_id": item["ledger_line_id"], "source_upload_id": upload.id,
                        "schedule_type": schedule_type.value, "as_of_date": as_of_date.isoformat(),
                        "bucket": item["bucket"], "open_balance": item["open_balance"],
                        "explanation_on_file": item["explanation_on_file"],
                    }, status=models.RiskFlagStatus.SYSTEM_FLAGGED,
                )
                tenant.add(flag)
                tenant.flush()
                audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                    action=AuditAction.AUDIT_RISK_FLAG_SYSTEM_GENERATED,
                    entity_type="RiskFlag", entity_id=flag.id,
                    metadata={"engagement_id": engagement.id, "source_schedule_id": schedule.id,
                              "ledger_line_id": item["ledger_line_id"], "status": flag.status.value,
                              "system_note": note},
                    ip_address=client_ip(request), user_agent=user_agent(request))
                flags_generated += 1
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.AUDIT_LEDGER_UPLOADED, entity_type="LedgerUpload",
            entity_id=upload.id, metadata={"engagement_id": engagement.id, "version": version,
            "rows": upload.row_count, "sha256": upload.file_sha256,
            "filename": upload.original_filename, "as_of_date": as_of_date.isoformat()},
            ip_address=client_ip(request), user_agent=user_agent(request))
        tenant.commit()
        committed = True
        tenant.refresh(upload)
        for schedule in schedules:
            tenant.refresh(schedule)
        return schemas.LedgerUploadResultOut(
            accepted=True, upload=schemas.LedgerUploadOut.model_validate(upload),
            schedules=[schemas.AgeingScheduleOut.model_validate(schedule) for schedule in schedules],
            ageing_flags_generated=flags_generated,
        )
    except Exception:
        tenant.rollback()
        if not committed:
            try:
                storage.delete(key, firm_id=tenant.firm_id)
            except StorageError:
                logger.exception("Could not remove ledger file after failed database write")
        raise


@router.get("/{engagement_id}/ageing-schedules", response_model=list[schemas.AgeingScheduleOut])
def list_ageing_schedules(engagement_id: str,
                          schedule_type: models.AgeingType | None = None,
                          limit: int = Query(100, ge=1, le=500),
                          _: models.User = Depends(require_role(Role.ASSOCIATE)),
                          tenant: TenantSession = Depends(get_tenant)):
    tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    query = tenant.query(models.AgeingSchedule).filter(
        models.AgeingSchedule.engagement_id == engagement_id,
    )
    if schedule_type:
        query = query.filter(models.AgeingSchedule.schedule_type == schedule_type)
    return query.order_by(models.AgeingSchedule.generated_at.desc()).limit(limit).all()


@router.get("/{engagement_id}/ledger", response_model=list[schemas.LedgerUploadOut])
def list_ledger_uploads(engagement_id: str,
                        _: models.User = Depends(require_role(Role.ASSOCIATE)),
                        tenant: TenantSession = Depends(get_tenant)):
    tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    return tenant.query(models.LedgerUpload).filter(
        models.LedgerUpload.engagement_id == engagement_id,
    ).order_by(models.LedgerUpload.version.desc()).all()


@router.get("/{engagement_id}/ledger/{upload_id}/file")
def download_ledger_source(engagement_id: str, upload_id: str,
                           _: models.User = Depends(require_role(Role.ASSOCIATE)),
                           tenant: TenantSession = Depends(get_tenant)):
    tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    upload = tenant.get(models.LedgerUpload, upload_id)
    if upload is None or upload.engagement_id != engagement_id:
        raise HTTPException(404, "Ledger upload not found")
    try:
        data = get_storage().load(upload.file_path, firm_id=tenant.firm_id)
    except FileNotFoundInStorage as exc:
        raise HTTPException(404, "The original ledger file is no longer available.") from exc
    except StorageError as exc:
        logger.exception("Could not load encrypted audit ledger source")
        raise HTTPException(500, "Could not load the stored audit ledger file.") from exc
    filename = quote(upload.original_filename, safe="")
    media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if upload.original_filename.lower().endswith(".xlsx") else "text/csv"
    return Response(content=data, media_type=media,
                     headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})
