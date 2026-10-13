"""Partner-confirmed materiality and CARO applicability planning gates."""
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.services.audit_assessments import assess_caro, calculate_materiality
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/audit-engagements", tags=["audit-planning-assessments"])


def _engagement(tenant, engagement_id):
    return tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")


def _planning(engagement):
    if engagement.status != models.AuditEngagementStatus.PLANNING:
        raise HTTPException(409, "Assessments can only be changed during Planning.")


def _editable_audit_work(engagement):
    if engagement.status not in (models.AuditEngagementStatus.PLANNING,
                                  models.AuditEngagementStatus.FIELDWORK,
                                  models.AuditEngagementStatus.COMPLETION):
        raise HTTPException(409, "Statutory-dues evidence cannot be changed after Reporting begins.")


def _partner(engagement, actor):
    if actor.id != engagement.engagement_partner_id or actor.role != models.UserRole.PARTNER:
        raise HTTPException(403, "Only the assigned engagement partner can confirm this assessment.")


def _fy_start(fy):
    return date(int(fy[:4]), 4, 1)


def _next_version(tenant, model, engagement_id):
    row = tenant.query(model).filter(model.engagement_id == engagement_id).order_by(model.version.desc()).first()
    return row.version + 1 if row else 1


@router.post("/{engagement_id}/materiality/suggest", response_model=schemas.MaterialityAssessmentOut)
def suggest_materiality(engagement_id: str, payload: schemas.MaterialitySuggestIn, request: Request,
                        actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                        tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    _planning(engagement)
    policy = tenant.query(models.FirmAuditPolicy).filter(
        models.FirmAuditPolicy.status == models.AuditPolicyStatus.APPROVED,
        models.FirmAuditPolicy.effective_from <= _fy_start(engagement.financial_year),
        (models.FirmAuditPolicy.effective_to.is_(None) |
         (models.FirmAuditPolicy.effective_to >= _fy_start(engagement.financial_year))),
    ).order_by(models.FirmAuditPolicy.effective_from.desc(), models.FirmAuditPolicy.version.desc()).first()
    if not policy:
        raise HTTPException(409, "No approved firm audit policy is effective for this financial year.")
    basis = payload.basis or policy.materiality_default_basis
    if basis not in policy.materiality_percentages:
        raise HTTPException(422, "The selected basis has no percentage configured in the effective firm policy.")
    if basis != policy.materiality_default_basis and not payload.basis_rationale:
        raise HTTPException(422, "Explain why a non-default materiality basis was selected.")
    inputs = {"turnover": payload.turnover, "profit_before_tax": payload.profit_before_tax,
              "net_worth": payload.net_worth}
    basis_amount = inputs[basis.value.lower()]
    percentage = Decimal(str(policy.materiality_percentages[basis.value]))
    try:
        suggested = calculate_materiality(basis_amount, percentage)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    for row in tenant.query(models.MaterialityAssessment).filter_by(engagement_id=engagement.id):
        if row.status == models.AuditAssessmentStatus.PROPOSED:
            row.status = models.AuditAssessmentStatus.SUPERSEDED
    assessment = models.MaterialityAssessment(
        engagement_id=engagement.id, firm_policy_id=policy.id,
        version=_next_version(tenant, models.MaterialityAssessment, engagement.id),
        status=models.AuditAssessmentStatus.PROPOSED, basis=basis, basis_amount=basis_amount,
        percentage=percentage, suggested_amount=suggested,
        inputs={k: str(v) for k, v in inputs.items()}, basis_rationale=payload.basis_rationale,
        created_by=actor.id,
    )
    tenant.add(assessment)
    tenant.flush()
    # A fresh proposal invalidates the previously approved figure until the
    # partner has reviewed this version, so downstream work cannot reuse stale materiality.
    engagement.materiality_amount = None
    engagement.materiality_basis = None
    engagement.materiality_assessment_id = assessment.id
    engagement.materiality_confirmed_by = None
    engagement.materiality_confirmed_at = None
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.MATERIALITY_SUGGESTED, entity_type="MaterialityAssessment",
        entity_id=assessment.id, metadata={"engagement_id": engagement.id, "basis": basis.value,
        "percentage": str(percentage), "suggested_amount": str(suggested), "inputs": assessment.inputs},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(assessment)
    return assessment


@router.post("/{engagement_id}/materiality/confirm", response_model=schemas.MaterialityAssessmentOut)
def confirm_materiality(engagement_id: str, payload: schemas.MaterialityConfirmIn, request: Request,
                        actor: models.User = Depends(require_role(Role.PARTNER)),
                        tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    _planning(engagement)
    _partner(engagement, actor)
    assessment = tenant.query(models.MaterialityAssessment).filter_by(
        engagement_id=engagement.id, status=models.AuditAssessmentStatus.PROPOSED).order_by(models.MaterialityAssessment.version.desc()).first()
    if assessment is None or assessment.status != models.AuditAssessmentStatus.PROPOSED:
        raise HTTPException(409, "There is no proposed materiality assessment to confirm.")
    amount = payload.confirmed_amount or assessment.suggested_amount
    overridden = amount != assessment.suggested_amount
    if overridden and not payload.override_rationale:
        raise HTTPException(422, "An override requires a rationale.")
    assessment.status = models.AuditAssessmentStatus.OVERRIDDEN if overridden else models.AuditAssessmentStatus.CONFIRMED
    assessment.confirmed_amount, assessment.confirmed_by = amount, actor.id
    assessment.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    assessment.override_rationale = payload.override_rationale
    engagement.materiality_amount = amount
    engagement.materiality_basis = f"{assessment.basis.value}; firm policy {assessment.firm_policy_id}"
    engagement.materiality_assessment_id = assessment.id
    engagement.materiality_confirmed_by = actor.id
    engagement.materiality_confirmed_at = assessment.confirmed_at
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.MATERIALITY_CONFIRMED, entity_type="MaterialityAssessment",
        entity_id=assessment.id, metadata={"suggested_amount": str(assessment.suggested_amount),
        "confirmed_amount": str(amount), "overridden": overridden, "rationale": payload.override_rationale},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(assessment)
    return assessment


@router.post("/{engagement_id}/caro-applicability/assess", response_model=schemas.CAROApplicabilityAssessmentOut)
def assess_caro_applicability(engagement_id: str, payload: schemas.CAROApplicabilityAssessIn, request: Request,
                              actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                              tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    _planning(engagement)
    rule = tenant.query_reference(models.CAROApplicabilityRule).filter(
        models.CAROApplicabilityRule.effective_from <= _fy_start(engagement.financial_year),
        (models.CAROApplicabilityRule.effective_to.is_(None) |
         (models.CAROApplicabilityRule.effective_to >= _fy_start(engagement.financial_year))),
    ).order_by(models.CAROApplicabilityRule.effective_from.desc(), models.CAROApplicabilityRule.version.desc()).first()
    if rule is None:
        raise HTTPException(409, "No CARO applicability rule is configured for this financial year.")
    inputs = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in payload.model_dump().items()}
    try:
        applicable, clauses, reasoning = assess_caro(rule.rule_data, inputs)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(409, "The effective CARO rule data is incomplete or invalid.") from exc
    for row in tenant.query(models.CAROApplicabilityAssessment).filter_by(engagement_id=engagement.id):
        if row.status == models.AuditAssessmentStatus.PROPOSED:
            row.status = models.AuditAssessmentStatus.SUPERSEDED
    assessment = models.CAROApplicabilityAssessment(
        engagement_id=engagement.id, rule_id=rule.id,
        version=_next_version(tenant, models.CAROApplicabilityAssessment, engagement.id),
        status=models.AuditAssessmentStatus.PROPOSED, determined_applicable=applicable,
        applicable_clause_numbers=clauses, reasoning=reasoning, inputs=inputs, assessed_by=actor.id,
    )
    tenant.add(assessment)
    tenant.flush()
    # Invalidate any older partner determination while the new rule assessment
    # awaits review; a previous CARO scope cannot silently carry forward.
    engagement.caro_applicable = None
    engagement.caro_applicability_reasoning = None
    engagement.caro_assessment_id = assessment.id
    engagement.caro_confirmed_by = None
    engagement.caro_confirmed_at = None
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.CARO_APPLICABILITY_ASSESSED, entity_type="CAROApplicabilityAssessment",
        entity_id=assessment.id, metadata={"engagement_id": engagement.id, "rule_version": rule.version,
        "determined_applicable": applicable, "clauses": clauses, "reasoning": reasoning, "inputs": inputs},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(assessment)
    return assessment


@router.post("/{engagement_id}/caro-applicability/confirm", response_model=schemas.CAROApplicabilityAssessmentOut)
def confirm_caro_applicability(engagement_id: str, payload: schemas.CAROApplicabilityConfirmIn, request: Request,
                               actor: models.User = Depends(require_role(Role.PARTNER)),
                               tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    _planning(engagement)
    _partner(engagement, actor)
    assessment = tenant.query(models.CAROApplicabilityAssessment).filter_by(
        engagement_id=engagement.id, status=models.AuditAssessmentStatus.PROPOSED).order_by(models.CAROApplicabilityAssessment.version.desc()).first()
    if assessment is None or assessment.status != models.AuditAssessmentStatus.PROPOSED:
        raise HTTPException(409, "There is no proposed CARO applicability assessment to confirm.")
    overridden = payload.applicable != assessment.determined_applicable
    if overridden and not payload.rationale:
        raise HTTPException(422, "An applicability override requires a rationale.")
    assessment.status = models.AuditAssessmentStatus.OVERRIDDEN if overridden else models.AuditAssessmentStatus.CONFIRMED
    assessment.confirmed_applicable, assessment.confirmed_by = payload.applicable, actor.id
    assessment.confirmed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    assessment.confirmation_rationale = payload.rationale
    engagement.caro_applicable = payload.applicable
    engagement.caro_applicability_reasoning = assessment.reasoning + (f" Partner decision: {payload.rationale}" if payload.rationale else " Partner confirmed the system determination.")
    engagement.caro_assessment_id = assessment.id
    engagement.caro_confirmed_by = actor.id
    engagement.caro_confirmed_at = assessment.confirmed_at
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.CARO_APPLICABILITY_CONFIRMED, entity_type="CAROApplicabilityAssessment",
        entity_id=assessment.id, metadata={"determined_applicable": assessment.determined_applicable,
        "confirmed_applicable": payload.applicable, "overridden": overridden, "rationale": payload.rationale},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(assessment)
    return assessment


@router.post("/{engagement_id}/start-fieldwork", response_model=schemas.AuditEngagementOut)
def start_fieldwork(engagement_id: str, request: Request,
                    actor: models.User = Depends(require_role(Role.PARTNER)),
                    tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    _planning(engagement)
    _partner(engagement, actor)
    if not engagement.materiality_amount or not engagement.materiality_assessment_id or not engagement.materiality_confirmed_by:
        raise HTTPException(409, "Partner-confirmed materiality is required before Fieldwork.")
    if engagement.caro_applicable is None or not engagement.caro_assessment_id or not engagement.caro_confirmed_by:
        raise HTTPException(409, "Partner-confirmed CARO applicability is required before Fieldwork.")
    if engagement.materiality_confirmed_by != actor.id or engagement.caro_confirmed_by != actor.id:
        raise HTTPException(409, "Both planning confirmations must be recorded by the assigned engagement partner.")
    engagement.status = models.AuditEngagementStatus.FIELDWORK
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.AUDIT_FIELDWORK_STARTED, entity_type="AuditEngagement",
        entity_id=engagement.id, metadata={"materiality_assessment_id": engagement.materiality_assessment_id,
        "caro_assessment_id": engagement.caro_assessment_id},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(engagement)
    return engagement


@router.get("/{engagement_id}/risk-flags", response_model=list[schemas.RiskFlagOut])
def list_risk_flags(engagement_id: str,
                    flag_status: models.RiskFlagStatus | None = Query(None, alias="status"),
                    limit: int = Query(200, ge=1, le=500),
                    _: models.User = Depends(require_role(Role.ASSOCIATE)),
                    tenant: TenantSession = Depends(get_tenant)):
    """Read tenant-scoped system observations with their source movements."""
    _engagement(tenant, engagement_id)
    query = tenant.query(models.RiskFlag).filter(models.RiskFlag.engagement_id == engagement_id)
    if flag_status:
        query = query.filter(models.RiskFlag.status == flag_status)
    return query.order_by(models.RiskFlag.created_at.desc()).limit(limit).all()


def _fy_periods(financial_year: str) -> list[str]:
    start_year = int(financial_year[:4])
    return [f"{year:04d}-{month:02d}"
            for year, month in [*((start_year, m) for m in range(4, 13)),
                                *((start_year + 1, m) for m in range(1, 4))]]


def _gstr2b_reconciliation_context(tenant, client_id: str, period: str) -> list[dict]:
    """Expose invoice-reconciliation availability without claiming tax filing proof."""
    gstins = tenant.query(models.ClientGSTIN).filter(
        models.ClientGSTIN.client_id == client_id,
    ).all()
    context = []
    for gstin in gstins:
        fetch = tenant.query(models.GSTR2BFetch).filter(
            models.GSTR2BFetch.client_gstin_id == gstin.id,
            models.GSTR2BFetch.period == period,
            models.GSTR2BFetch.is_current.is_(True),
        ).order_by(models.GSTR2BFetch.version.desc()).first()
        run = tenant.query(models.ReconciliationRun).filter(
            models.ReconciliationRun.client_gstin_id == gstin.id,
            models.ReconciliationRun.period == period,
            models.ReconciliationRun.is_current.is_(True),
            models.ReconciliationRun.status == models.RunStatus.COMPLETED,
        ).order_by(models.ReconciliationRun.version.desc()).first()
        context.append({
            "gstin_id": gstin.id,
            "gstin": gstin.gstin,
            "gstr2b_available": fetch is not None,
            "gstr2b_fetched_at": fetch.fetched_at if fetch else None,
            "reconciliation_available": run is not None,
            "scope_note": "Invoice-level GSTR-2B context only; it does not establish client return filing or tax payment.",
        })
    return context


@router.get("/{engagement_id}/statutory-dues", response_model=list[schemas.StatutoryDuesPeriodOut])
def get_statutory_dues(engagement_id: str,
                       _: models.User = Depends(require_role(Role.ASSOCIATE)),
                       tenant: TenantSession = Depends(get_tenant)):
    """Return a FY-period grid; missing source records stay explicitly unverified."""
    engagement = _engagement(tenant, engagement_id)
    current = tenant.query(models.StatutoryFilingRecord).filter(
        models.StatutoryFilingRecord.engagement_id == engagement.id,
        models.StatutoryFilingRecord.is_current.is_(True),
    ).all()
    records = {(row.period, row.tax_type): row for row in current}
    output = []
    for period in _fy_periods(engagement.financial_year):
        context = _gstr2b_reconciliation_context(tenant, engagement.client_id, period)
        for tax_type in models.StatutoryTaxType:
            record = records.get((period, tax_type))
            output.append({
                "period": period,
                "tax_type": tax_type,
                "filing_status": record.filing_status if record else None,
                "verification_status": "NOT_RECORDED" if record is None else ("REVIEWED" if record.reviewed_by else "UNVERIFIED"),
                "current_record": record,
                # GSTR-2B is shown as reconciliation context only. It does not
                # prove the audit client's GST return filing or dues payment.
                "gstr2b_context": context if tax_type == models.StatutoryTaxType.GST else [],
            })
    return output


@router.post("/{engagement_id}/statutory-dues", response_model=schemas.StatutoryFilingRecordOut, status_code=201)
def record_statutory_filing(engagement_id: str, payload: schemas.StatutoryFilingRecordCreate,
                            request: Request,
                            actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                            tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    _editable_audit_work(engagement)
    first = _fy_periods(engagement.financial_year)[0]
    last = _fy_periods(engagement.financial_year)[-1]
    if not first <= payload.period <= last:
        raise HTTPException(422, "Filing period must fall within this engagement's financial year.")
    if payload.filing_status == models.StatutoryFilingStatus.FILED and not (payload.filed_at or payload.source_reference):
        raise HTTPException(422, "A filed status requires a filing date or source reference.")
    if payload.filing_status in (models.StatutoryFilingStatus.PENDING, models.StatutoryFilingStatus.ARREARS) and not (payload.statutory_due_date and (payload.source_reference or payload.notes)):
        raise HTTPException(422, "Pending or arrears status requires a due date and supporting source or note.")
    versions = tenant.query(models.StatutoryFilingRecord).filter(
        models.StatutoryFilingRecord.engagement_id == engagement.id,
        models.StatutoryFilingRecord.tax_type == payload.tax_type,
        models.StatutoryFilingRecord.period == payload.period,
    ).order_by(models.StatutoryFilingRecord.version.desc()).all()
    for previous in versions:
        if previous.is_current:
            previous.is_current = False
    row = models.StatutoryFilingRecord(
        engagement_id=engagement.id,
        tax_type=payload.tax_type,
        period=payload.period,
        version=(versions[0].version + 1) if versions else 1,
        is_current=True,
        filing_status=payload.filing_status,
        filing_source=payload.filing_source,
        filed_at=payload.filed_at,
        statutory_due_date=payload.statutory_due_date,
        amount_due=payload.amount_due,
        amount_paid=payload.amount_paid,
        source_reference=payload.source_reference,
        notes=payload.notes,
        recorded_by=actor.id,
    )
    tenant.add(row)
    tenant.flush()
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.STATUTORY_FILING_RECORDED, entity_type="StatutoryFilingRecord",
        entity_id=row.id, metadata={"engagement_id": engagement.id, "tax_type": row.tax_type.value,
        "period": row.period, "version": row.version, "filing_status": row.filing_status.value,
        "filing_source": row.filing_source.value, "source_reference": row.source_reference,
        "status_note": "Source reported; not independently verified."},
        ip_address=client_ip(request), user_agent=user_agent(request))
    if row.filing_status == models.StatutoryFilingStatus.ARREARS:
        flag = models.RiskFlag(
            engagement_id=engagement.id,
            policy_version=None,
            flag_type=models.RiskFlagType.STATUTORY_DUES,
            severity="REVIEW_REQUIRED",
            status=models.RiskFlagStatus.SYSTEM_FLAGGED,
            system_note=(f"SYSTEM FLAG — {row.tax_type.value} for {row.period} was reported as arrears "
                         f"from {row.filing_source.value}. Verify against the cited source; this is not a CARO conclusion."),
            trigger_data={"statutory_filing_record_id": row.id, "period": row.period,
                          "tax_type": row.tax_type.value, "reported_status": row.filing_status.value,
                          "filing_source": row.filing_source.value, "source_reference": row.source_reference},
        )
        tenant.add(flag)
        tenant.flush()
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.AUDIT_RISK_FLAG_SYSTEM_GENERATED, entity_type="RiskFlag",
            entity_id=flag.id, metadata={"engagement_id": engagement.id,
            "source_record_id": row.id, "flag_type": "STATUTORY_DUES", "status": "SYSTEM_FLAGGED"},
            ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row


@router.get("/{engagement_id}/statutory-dues/records", response_model=list[schemas.StatutoryFilingRecordOut])
def list_statutory_filing_history(engagement_id: str,
                                 limit: int = Query(200, ge=1, le=1000),
                                 _: models.User = Depends(require_role(Role.ASSOCIATE)),
                                 tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    return tenant.query(models.StatutoryFilingRecord).filter(
        models.StatutoryFilingRecord.engagement_id == engagement.id,
    ).order_by(models.StatutoryFilingRecord.period.desc(),
               models.StatutoryFilingRecord.tax_type.asc(),
               models.StatutoryFilingRecord.version.desc()).limit(limit).all()


@router.post("/{engagement_id}/statutory-dues/{record_id}/review", response_model=schemas.StatutoryFilingRecordOut)
def review_statutory_filing(engagement_id: str, record_id: str, request: Request,
                            actor: models.User = Depends(require_role(Role.MANAGER)),
                            tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    row = tenant.query(models.StatutoryFilingRecord).filter(
        models.StatutoryFilingRecord.id == record_id,
        models.StatutoryFilingRecord.engagement_id == engagement.id,
        models.StatutoryFilingRecord.is_current.is_(True),
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "Current statutory filing record not found.")
    if row.recorded_by == actor.id:
        raise HTTPException(409, "You cannot review your own statutory filing record.")
    if row.reviewed_by:
        raise HTTPException(409, "This filing record has already been reviewed.")
    row.reviewed_by = actor.id
    row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.STATUTORY_FILING_REVIEWED, entity_type="StatutoryFilingRecord",
        entity_id=row.id, metadata={"engagement_id": engagement.id, "period": row.period,
        "tax_type": row.tax_type.value, "version": row.version},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row
