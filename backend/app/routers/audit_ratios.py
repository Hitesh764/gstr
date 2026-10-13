"""Mapped, mechanical audit ratios with review and partner approval."""
from datetime import date, datetime, timezone
from decimal import Decimal
from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.services.audit_ratios import calculate_audit_ratios
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/audit-engagements", tags=["audit-ratios"])


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _engagement(tenant, engagement_id):
    return tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")


def _fy_start(fy):
    return date(int(fy[:4]), 4, 1)


def _effective_policy(tenant, engagement):
    date_in_force = _fy_start(engagement.financial_year)
    return tenant.query(models.FirmAuditPolicy).filter(
        models.FirmAuditPolicy.status == models.AuditPolicyStatus.APPROVED,
        models.FirmAuditPolicy.effective_from <= date_in_force,
        (models.FirmAuditPolicy.effective_to.is_(None) |
         (models.FirmAuditPolicy.effective_to >= date_in_force)),
    ).order_by(models.FirmAuditPolicy.effective_from.desc(),
               models.FirmAuditPolicy.version.desc()).first()


@router.post("/{engagement_id}/ratio-analyses", response_model=schemas.AuditRatioAnalysisOut)
def generate_ratio_analysis(engagement_id: str, payload: schemas.AuditRatioAnalysisCreate,
                            request: Request,
                            actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                            tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    if engagement.status not in (models.AuditEngagementStatus.PLANNING,
                                  models.AuditEngagementStatus.FIELDWORK,
                                  models.AuditEngagementStatus.COMPLETION):
        raise HTTPException(409, "Ratio analysis cannot be generated after Reporting begins.")
    policy = _effective_policy(tenant, engagement)
    if policy is None:
        raise HTTPException(409, "An effective approved firm policy is required for the ratio movement threshold.")
    closing_upload = tenant.query(models.TrialBalanceUpload).filter(
        models.TrialBalanceUpload.engagement_id == engagement.id,
        models.TrialBalanceUpload.period_type == models.TrialBalancePeriodType.CLOSING,
        models.TrialBalanceUpload.is_current.is_(True),
    ).first()
    if closing_upload is None:
        raise HTTPException(409, "Upload a current and comparative closing trial balance before ratio analysis.")
    closing_lines = tenant.query(models.TrialBalanceLine).filter_by(upload_id=closing_upload.id).all()
    if not closing_lines:
        raise HTTPException(409, "The current trial balance contains no mapped account lines.")
    mapping = {key.removesuffix("_account_codes"): value for key, value in payload.model_dump().items()
               if key.endswith("_account_codes")}
    requested_codes = {code.casefold() for values in mapping.values() for code in values}
    present_codes = {line.account_code.casefold() for line in closing_lines}
    missing = sorted(requested_codes - present_codes)
    if missing:
        raise HTTPException(422, {"message": "Ratio mapping contains account codes absent from the current trial balance.", "account_codes": missing})
    opening_upload = tenant.query(models.TrialBalanceUpload).filter(
        models.TrialBalanceUpload.engagement_id == engagement.id,
        models.TrialBalanceUpload.period_type == models.TrialBalancePeriodType.OPENING,
        models.TrialBalanceUpload.is_current.is_(True),
    ).first()
    opening_lines = tenant.query(models.TrialBalanceLine).filter_by(upload_id=opening_upload.id).all() if opening_upload else []
    results = calculate_audit_ratios(
        closing_lines=closing_lines, opening_lines=opening_lines,
        account_mapping=mapping,
        prior_opening_inventory=payload.prior_opening_inventory,
        prior_opening_receivables=payload.prior_opening_receivables,
        movement_threshold_pct=Decimal(policy.ratio_movement_threshold_pct),
    )
    version_row = tenant.query(models.AuditRatioAnalysis).filter_by(
        engagement_id=engagement.id,
    ).order_by(models.AuditRatioAnalysis.version.desc()).first()
    mapping_snapshot = {
        **mapping,
        "prior_opening_inventory": str(payload.prior_opening_inventory) if payload.prior_opening_inventory is not None else None,
        "prior_opening_receivables": str(payload.prior_opening_receivables) if payload.prior_opening_receivables is not None else None,
    }
    analysis = models.AuditRatioAnalysis(
        engagement_id=engagement.id, version=(version_row.version + 1) if version_row else 1,
        status=models.RatioAnalysisStatus.DRAFT,
        closing_upload_id=closing_upload.id,
        opening_upload_id=opening_upload.id if opening_upload else None,
        prior_opening_source_reference=payload.prior_opening_source_reference,
        account_mapping=mapping_snapshot,
        ratio_results=results,
        movement_threshold_pct=policy.ratio_movement_threshold_pct,
        risk_flag_ids=[], prepared_by=actor.id,
    )
    tenant.add(analysis)
    tenant.flush()
    ratio_results = deepcopy(analysis.ratio_results)
    generated_flag_ids = []
    for name, values in ratio_results["ratios"].items():
        if not values["threshold_exceeded"]:
            continue
        current = values["current_year"]
        prior = values["prior_year"]
        movement = values["movement_pct"]
        note = (f"SYSTEM FLAG — {name.replace('_', ' ')} moved from {prior} to {current} "
                f"({movement}% year-on-year), exceeding the configured {policy.ratio_movement_threshold_pct}% movement threshold. "
                "Review the mapping, source balances, and explanation; no conclusion about normality or cause is generated.")
        flag = models.RiskFlag(
            engagement_id=engagement.id, policy_version=policy.version,
            flag_type=models.RiskFlagType.RATIO, severity="REVIEW_REQUIRED",
            system_note=note, status=models.RiskFlagStatus.SYSTEM_FLAGGED,
            trigger_data={"subtype": "RATIO_MOVEMENT", "ratio_name": name,
                          "current_year": current, "prior_year": prior,
                          "movement_pct": movement,
                          "movement_threshold_pct": str(policy.ratio_movement_threshold_pct),
                          "analysis_version": analysis.version},
        )
        tenant.add(flag)
        tenant.flush()
        generated_flag_ids.append(flag.id)
        ratio_results["ratios"][name]["risk_flag_id"] = flag.id
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.AUDIT_RISK_FLAG_SYSTEM_GENERATED,
            entity_type="RiskFlag", entity_id=flag.id,
            metadata={"engagement_id": engagement.id, "analysis_id": analysis.id,
                      "ratio_name": name, "status": flag.status.value,
                      "system_note": note, "trigger_data": flag.trigger_data},
            ip_address=client_ip(request), user_agent=user_agent(request))
    analysis.risk_flag_ids = generated_flag_ids
    analysis.ratio_results = ratio_results
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.AUDIT_RATIO_ANALYSIS_GENERATED,
        entity_type="AuditRatioAnalysis", entity_id=analysis.id,
        metadata={"engagement_id": engagement.id, "version": analysis.version,
                  "closing_upload_id": closing_upload.id,
                  "opening_upload_id": analysis.opening_upload_id,
                  "movement_threshold_pct": str(policy.ratio_movement_threshold_pct),
                  "mapping": mapping_snapshot, "results": analysis.ratio_results,
                  "risk_flag_ids": generated_flag_ids},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(analysis)
    return analysis


@router.get("/{engagement_id}/ratio-analyses", response_model=list[schemas.AuditRatioAnalysisOut])
def list_ratio_analyses(engagement_id: str, limit: int = Query(100, ge=1, le=500),
                        _: models.User = Depends(require_role(Role.ASSOCIATE)),
                        tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    return tenant.query(models.AuditRatioAnalysis).filter(
        models.AuditRatioAnalysis.engagement_id == engagement_id,
    ).order_by(models.AuditRatioAnalysis.version.desc()).limit(limit).all()


@router.post("/{engagement_id}/ratio-analyses/{analysis_id}/review",
             response_model=schemas.AuditRatioAnalysisOut)
def review_ratio_analysis(engagement_id: str, analysis_id: str, request: Request,
                          actor: models.User = Depends(require_role(Role.MANAGER)),
                          tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    analysis = tenant.get(models.AuditRatioAnalysis, analysis_id)
    if analysis is None or analysis.engagement_id != engagement.id:
        raise HTTPException(404, "Ratio analysis not found")
    if analysis.status != models.RatioAnalysisStatus.DRAFT:
        raise HTTPException(409, "Only a draft ratio analysis can be reviewed.")
    if analysis.prepared_by == actor.id:
        raise HTTPException(409, "A preparer cannot review their own ratio analysis.")
    analysis.status = models.RatioAnalysisStatus.MANAGER_REVIEWED
    analysis.reviewed_by, analysis.reviewed_at = actor.id, _now()
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.AUDIT_RATIO_ANALYSIS_REVIEWED,
        entity_type="AuditRatioAnalysis", entity_id=analysis.id,
        metadata={"engagement_id": engagement.id, "prepared_by": analysis.prepared_by,
                  "reviewed_by": actor.id, "version": analysis.version},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(analysis)
    return analysis


@router.post("/{engagement_id}/ratio-analyses/{analysis_id}/approve",
             response_model=schemas.AuditRatioAnalysisOut)
def approve_ratio_analysis(engagement_id: str, analysis_id: str, request: Request,
                           actor: models.User = Depends(require_role(Role.PARTNER)),
                           tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    analysis = tenant.get(models.AuditRatioAnalysis, analysis_id)
    if analysis is None or analysis.engagement_id != engagement.id:
        raise HTTPException(404, "Ratio analysis not found")
    if actor.id != engagement.engagement_partner_id:
        raise HTTPException(403, "Only the assigned engagement partner can approve this analysis.")
    if analysis.status != models.RatioAnalysisStatus.MANAGER_REVIEWED or not analysis.reviewed_by:
        raise HTTPException(409, "Manager review is required before partner approval.")
    if actor.id in (analysis.prepared_by, analysis.reviewed_by):
        raise HTTPException(409, "The partner cannot approve an analysis they prepared or reviewed.")
    analysis.status = models.RatioAnalysisStatus.APPROVED
    analysis.approved_by, analysis.approved_at = actor.id, _now()
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.AUDIT_RATIO_ANALYSIS_APPROVED,
        entity_type="AuditRatioAnalysis", entity_id=analysis.id,
        metadata={"engagement_id": engagement.id, "prepared_by": analysis.prepared_by,
                  "reviewed_by": analysis.reviewed_by, "approved_by": actor.id,
                  "version": analysis.version},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(analysis)
    return analysis
