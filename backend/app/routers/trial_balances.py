"""Versioned encrypted trial-balance uploads for audit engagements."""
import hashlib
import logging
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.ingestion.trial_balance import parse_trial_balance_file
from app.services.trial_balance import compare_trial_balances
from app.services.audit_assessments import variance_flag_candidate, variance_system_note
from app.services.related_party_matching import recheck_engagement
from app.storage import FileNotFoundInStorage, StorageError, get_storage
from app.tenancy import TenantSession

logger = logging.getLogger("gstr2b.audit.trial_balance")
router = APIRouter(prefix="/api/audit-engagements", tags=["trial-balances"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = (".csv", ".xlsx")


async def _read_upload(file: UploadFile) -> bytes:
    filename = file.filename or ""
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(415, "Unsupported file type. Upload a .csv or .xlsx file.")
    contents = await file.read(MAX_UPLOAD_BYTES + 1)
    if not contents:
        raise HTTPException(422, "The uploaded file is empty.")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Each trial-balance file is limited to 10 MB.")
    return contents


def _error_rows(outcome, prefix: str = "") -> list[schemas.RowError]:
    return [
        schemas.RowError(
            row=issue.row,
            field=(f"{prefix}.{issue.field}" if prefix and issue.field else issue.field),
            message=(f"{prefix}: {issue.message}" if prefix else issue.message),
            severity=issue.severity,
        )
        for issue in outcome.errors
    ]


def _next_version(tenant: TenantSession, engagement_id: str,
                  period_type: models.TrialBalancePeriodType) -> int:
    latest = tenant.query(models.TrialBalanceUpload).filter(
        models.TrialBalanceUpload.engagement_id == engagement_id,
        models.TrialBalanceUpload.period_type == period_type,
    ).order_by(models.TrialBalanceUpload.version.desc()).first()
    return latest.version + 1 if latest else 1


def _supersede_current(tenant: TenantSession, engagement_id: str,
                       period_type: models.TrialBalancePeriodType) -> None:
    current = tenant.query(models.TrialBalanceUpload).filter(
        models.TrialBalanceUpload.engagement_id == engagement_id,
        models.TrialBalanceUpload.period_type == period_type,
        models.TrialBalanceUpload.is_current.is_(True),
    ).all()
    for upload in current:
        upload.is_current = False


def _new_upload(*, engagement_id: str, period_type: models.TrialBalancePeriodType,
                version: int, filename: str, file_path: str, contents: bytes,
                uploaded_by: str, row_count: int) -> models.TrialBalanceUpload:
    return models.TrialBalanceUpload(
        engagement_id=engagement_id,
        period_type=period_type,
        version=version,
        is_current=True,
        original_filename=filename[:400],
        file_path=file_path,
        file_sha256=hashlib.sha256(contents).hexdigest(),
        row_count=row_count,
        uploaded_by=uploaded_by,
    )


def _delete_saved(storage, keys: list[str], firm_id: str) -> None:
    for key in keys:
        try:
            storage.delete(key, firm_id=firm_id)
        except StorageError:
            logger.exception("Could not remove trial-balance file after a failed database write")


def _record_upload(tenant: TenantSession, actor: models.User, upload: models.TrialBalanceUpload,
                   request: Request, *, companion_upload_id: str | None = None) -> None:
    audit.record(
        tenant.db,
        firm_id=tenant.firm_id,
        user_id=actor.id,
        action=AuditAction.TRIAL_BALANCE_UPLOADED,
        entity_type="TrialBalanceUpload",
        entity_id=upload.id,
        metadata={
            "engagement_id": upload.engagement_id,
            "period_type": upload.period_type.value,
            "version": upload.version,
            "rows": upload.row_count,
            "sha256": upload.file_sha256,
            "companion_upload_id": companion_upload_id,
            "filename": upload.original_filename,
        },
        ip_address=client_ip(request),
        user_agent=user_agent(request),
    )


@router.post("/{engagement_id}/trial-balances/compare",
             response_model=schemas.TrialBalanceCompareResultOut)
async def upload_comparative_trial_balance(
    engagement_id: str,
    request: Request,
    current_file: UploadFile = File(...),
    prior_file: UploadFile = File(...),
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Upload both year snapshots together and persist line-by-line movement.

    Both files are validated before either one is stored. On validation
    errors, the database and encrypted file storage remain untouched.
    """
    engagement = tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    fy_start = date(int(engagement.financial_year[:4]), 4, 1)
    policy = tenant.query(models.FirmAuditPolicy).filter(
        models.FirmAuditPolicy.status == models.AuditPolicyStatus.APPROVED,
        models.FirmAuditPolicy.effective_from <= fy_start,
        (models.FirmAuditPolicy.effective_to.is_(None) |
         (models.FirmAuditPolicy.effective_to >= fy_start)),
    ).order_by(models.FirmAuditPolicy.effective_from.desc(), models.FirmAuditPolicy.version.desc()).first()
    current_bytes = await _read_upload(current_file)
    prior_bytes = await _read_upload(prior_file)
    current_outcome = parse_trial_balance_file(current_bytes, current_file.filename or "")
    prior_outcome = parse_trial_balance_file(prior_bytes, prior_file.filename or "")
    errors = _error_rows(current_outcome, "current_file") + _error_rows(prior_outcome, "prior_file")
    if errors:
        return schemas.TrialBalanceCompareResultOut(accepted=False, errors=errors)

    merged, changes = compare_trial_balances(current_outcome.rows, prior_outcome.rows)
    storage = get_storage()
    saved_keys: list[str] = []
    committed = False
    try:
        current_key = storage.save(
            firm_id=tenant.firm_id, category="trial_balances", data=current_bytes,
        )
        saved_keys.append(current_key)
        prior_key = storage.save(
            firm_id=tenant.firm_id, category="trial_balances", data=prior_bytes,
        )
        saved_keys.append(prior_key)

        current_upload = _new_upload(
            engagement_id=engagement_id,
            period_type=models.TrialBalancePeriodType.CLOSING,
            version=_next_version(tenant, engagement_id, models.TrialBalancePeriodType.CLOSING),
            filename=current_file.filename or "current.csv",
            file_path=current_key,
            contents=current_bytes,
            uploaded_by=actor.id,
            row_count=len(merged),
        )
        prior_upload = _new_upload(
            engagement_id=engagement_id,
            period_type=models.TrialBalancePeriodType.COMPARATIVE,
            version=_next_version(tenant, engagement_id, models.TrialBalancePeriodType.COMPARATIVE),
            filename=prior_file.filename or "prior.csv",
            file_path=prior_key,
            contents=prior_bytes,
            uploaded_by=actor.id,
            row_count=len(prior_outcome.rows),
        )
        _supersede_current(tenant, engagement_id, models.TrialBalancePeriodType.CLOSING)
        _supersede_current(tenant, engagement_id, models.TrialBalancePeriodType.COMPARATIVE)
        tenant.add(current_upload)
        tenant.add(prior_upload)
        tenant.flush()
        current_upload.compares_to_upload_id = prior_upload.id

        for row in merged:
            tenant.add(models.TrialBalanceLine(upload_id=current_upload.id, **row))
        for row in prior_outcome.rows:
            tenant.add(models.TrialBalanceLine(
                upload_id=prior_upload.id,
                account_code=row["account_code"],
                account_name=row["account_name"],
                account_group=row["account_group"],
                current_year_amount=None,
                prior_year_amount=row["amount"],
                source_row_number=row["source_row_number"],
            ))

        _record_upload(tenant, actor, current_upload, request,
                       companion_upload_id=prior_upload.id)
        _record_upload(tenant, actor, prior_upload, request,
                       companion_upload_id=current_upload.id)
        tenant.flush()
        flag_count = 0
        if policy is not None:
            current_lines = tenant.query(models.TrialBalanceLine).filter_by(upload_id=current_upload.id).all()
            lines_by_group = {}
            for line in current_lines:
                if line.account_group:
                    lines_by_group.setdefault(line.account_group.casefold(), []).append(line)
            confirmed_materiality = (
                engagement.materiality_amount
                if engagement.materiality_confirmed_by == engagement.engagement_partner_id
                else None
            )
            for line in current_lines:
                qualifies, trigger_data = variance_flag_candidate(
                    line,
                    percentage_threshold=policy.variance_threshold_pct,
                    materiality_amount=confirmed_materiality,
                    materiality_multiplier=policy.variance_materiality_multiplier,
                )
                if not qualifies:
                    continue
                flag = models.RiskFlag(
                    engagement_id=engagement.id,
                    trial_balance_line_id=line.id,
                    policy_version=policy.version,
                    flag_type=models.RiskFlagType.VARIANCE,
                    severity="REVIEW_REQUIRED",
                    system_note=variance_system_note(
                        line, trigger_data, lines_by_group.get(line.account_group.casefold(), [])
                        if line.account_group else [],
                    ),
                    trigger_data=trigger_data,
                    status=models.RiskFlagStatus.SYSTEM_FLAGGED,
                )
                tenant.add(flag)
                tenant.flush()
                audit.record(
                    tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                    action=AuditAction.AUDIT_RISK_FLAG_SYSTEM_GENERATED,
                    entity_type="RiskFlag", entity_id=flag.id,
                    metadata={"engagement_id": engagement.id, "trial_balance_line_id": line.id,
                              "policy_version": policy.version, "status": flag.status.value,
                              "system_note": flag.system_note, "trigger_data": trigger_data},
                    ip_address=client_ip(request), user_agent=user_agent(request),
                )
                flag_count += 1
        # Matching is exact name/alias cross-reference only; it never resolves
        # prior flags or makes a disclosure conclusion.
        recheck_engagement(tenant, engagement, actor)
        tenant.commit()
        committed = True
        tenant.refresh(current_upload)
        tenant.refresh(prior_upload)
        return schemas.TrialBalanceCompareResultOut(
            accepted=True,
            current_upload=schemas.TrialBalanceUploadOut.model_validate(current_upload),
            prior_upload=schemas.TrialBalanceUploadOut.model_validate(prior_upload),
            inserted=len(merged),
            new_accounts=sorted(
                (item["account_code"] for item in changes if item["change"] == "NEW_CURRENT_YEAR"),
                key=str.casefold,
            ),
            prior_only_accounts=sorted(
                (item["account_code"] for item in changes if item["change"] == "ABSENT_CURRENT_YEAR"),
                key=str.casefold,
            ),
            risk_flags_generated=flag_count,
            risk_flags_status=("GENERATED" if policy is not None else "SKIPPED_NO_EFFECTIVE_APPROVED_POLICY"),
        )
    except IntegrityError as exc:
        tenant.rollback()
        if not committed:
            _delete_saved(storage, saved_keys, tenant.firm_id)
        raise HTTPException(409, "A concurrent trial-balance upload created a version conflict; retry.") from exc
    except Exception:
        tenant.rollback()
        if not committed:
            _delete_saved(storage, saved_keys, tenant.firm_id)
        raise


@router.post("/{engagement_id}/trial-balances/opening",
             response_model=schemas.TrialBalanceUploadResultOut)
async def upload_opening_trial_balance(
    engagement_id: str,
    request: Request,
    file: UploadFile = File(...),
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    contents = await _read_upload(file)
    outcome = parse_trial_balance_file(contents, file.filename or "")
    if outcome.errors:
        return schemas.TrialBalanceUploadResultOut(
            accepted=False, errors=_error_rows(outcome),
        )

    storage = get_storage()
    key = storage.save(firm_id=tenant.firm_id, category="trial_balances", data=contents)
    committed = False
    try:
        period_type = models.TrialBalancePeriodType.OPENING
        _supersede_current(tenant, engagement_id, period_type)
        upload = _new_upload(
            engagement_id=engagement_id,
            period_type=period_type,
            version=_next_version(tenant, engagement_id, period_type),
            filename=file.filename or "opening.csv",
            file_path=key,
            contents=contents,
            uploaded_by=actor.id,
            row_count=len(outcome.rows),
        )
        tenant.add(upload)
        tenant.flush()
        for row in outcome.rows:
            tenant.add(models.TrialBalanceLine(
                upload_id=upload.id,
                account_code=row["account_code"],
                account_name=row["account_name"],
                account_group=row["account_group"],
                current_year_amount=row["amount"],
                source_row_number=row["source_row_number"],
            ))
        _record_upload(tenant, actor, upload, request)
        tenant.commit()
        committed = True
        tenant.refresh(upload)
        return schemas.TrialBalanceUploadResultOut(
            accepted=True,
            upload=schemas.TrialBalanceUploadOut.model_validate(upload),
            inserted=len(outcome.rows),
        )
    except IntegrityError as exc:
        tenant.rollback()
        if not committed:
            _delete_saved(storage, [key], tenant.firm_id)
        raise HTTPException(409, "A concurrent trial-balance upload created a version conflict; retry.") from exc
    except Exception:
        tenant.rollback()
        if not committed:
            _delete_saved(storage, [key], tenant.firm_id)
        raise


@router.get("/{engagement_id}/trial-balances",
            response_model=list[schemas.TrialBalanceUploadOut])
def list_trial_balance_uploads(
    engagement_id: str,
    period_type: models.TrialBalancePeriodType | None = None,
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    query = tenant.query(models.TrialBalanceUpload).filter(
        models.TrialBalanceUpload.engagement_id == engagement_id,
    )
    if period_type:
        query = query.filter(models.TrialBalanceUpload.period_type == period_type)
    return query.order_by(
        models.TrialBalanceUpload.period_type.asc(),
        models.TrialBalanceUpload.version.desc(),
    ).all()


@router.get("/{engagement_id}/trial-balances/{upload_id}/lines",
            response_model=list[schemas.TrialBalanceLineOut])
def list_trial_balance_lines(
    engagement_id: str,
    upload_id: str,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    upload = tenant.get(models.TrialBalanceUpload, upload_id)
    if upload is None or upload.engagement_id != engagement_id:
        raise HTTPException(404, "Trial-balance upload not found")
    return tenant.query(models.TrialBalanceLine).filter(
        models.TrialBalanceLine.upload_id == upload.id,
    ).order_by(models.TrialBalanceLine.account_code.asc()).offset(offset).limit(limit).all()


@router.get("/{engagement_id}/trial-balances/{upload_id}/file")
def download_trial_balance_source(
    engagement_id: str,
    upload_id: str,
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    upload = tenant.get(models.TrialBalanceUpload, upload_id)
    if upload is None or upload.engagement_id != engagement_id:
        raise HTTPException(404, "Trial-balance upload not found")
    try:
        data = get_storage().load(upload.file_path, firm_id=tenant.firm_id)
    except FileNotFoundInStorage as exc:
        raise HTTPException(404, "The original trial-balance file is no longer available.") from exc
    except StorageError as exc:
        logger.exception("Could not load encrypted trial-balance source")
        raise HTTPException(500, "Could not load the stored trial-balance file.") from exc
    filename = quote(upload.original_filename, safe="")
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if upload.original_filename.lower().endswith(".xlsx") else "text/csv",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
