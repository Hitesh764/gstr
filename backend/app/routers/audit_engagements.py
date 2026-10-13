"""Create and read firm-scoped audit engagements."""
from typing import Optional
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.exc import IntegrityError

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/audit-engagements", tags=["audit-engagements"])


@router.get("/dashboard/summary")
def audit_dashboard_summary(
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Firm-wide dashboard summary, including deadlines and assigned workload."""
    active = tenant.query(models.AuditEngagement).filter(
        models.AuditEngagement.status != models.AuditEngagementStatus.CLOSED
    ).order_by(models.AuditEngagement.reporting_deadline.asc().nullslast()).all()
    result = []
    workload = {}
    for engagement in active:
        flags = tenant.query(models.RiskFlag).filter(
            models.RiskFlag.engagement_id == engagement.id,
            models.RiskFlag.status.in_([models.RiskFlagStatus.SYSTEM_FLAGGED, models.RiskFlagStatus.UNDER_REVIEW]),
        ).all()
        caro = tenant.query(models.CAROClauseResponse).filter(
            models.CAROClauseResponse.engagement_id == engagement.id,
        ).all()
        banks = tenant.query(models.BankConfirmation).filter(
            models.BankConfirmation.engagement_id == engagement.id,
            models.BankConfirmation.letter_status.in_([
                models.BankConfirmationStatus.DRAFTED, models.BankConfirmationStatus.SENT,
                models.BankConfirmationStatus.PENDING]),
        ).count()
        partner = tenant.get(models.User, engagement.engagement_partner_id)
        name = tenant.get(models.Client, engagement.client_id)
        days = (engagement.reporting_deadline - date.today()).days if engagement.reporting_deadline else None
        item = {"id": engagement.id, "client_id": engagement.client_id,
            "client_name": name.name if name else "Client", "financial_year": engagement.financial_year,
            "engagement_type": engagement.engagement_type.value, "status": engagement.status.value,
            "engagement_partner_id": engagement.engagement_partner_id,
            "partner_name": partner.name if partner else None,
            "reporting_deadline": engagement.reporting_deadline,
            "days_until_deadline": days,
            "open_risk_flags": len(flags),
            "high_severity_flags": sum(1 for flag in flags if flag.severity.upper() in ("HIGH", "CRITICAL")),
            "caro_total": len([r for r in caro if r.status != models.CAROResponseStatus.SUPERSEDED]),
            "caro_finalized": sum(1 for r in caro if r.status == models.CAROResponseStatus.FINALIZED),
            "bank_confirmations_outstanding": banks}
        result.append(item)
        workload[engagement.engagement_partner_id] = workload.get(engagement.engagement_partner_id, 0) + 1
        associate_id = name.assigned_associate_id if name else None
        if associate_id:
            workload[associate_id] = workload.get(associate_id, 0) + 1
    return {"active_engagements": result,
        "workload_by_staff": [{"staff_id": pid, "staff_name": (tenant.get(models.User, pid).name if tenant.get(models.User, pid) else None), "role": (tenant.get(models.User, pid).role.value if tenant.get(models.User, pid) else None), "engagement_count": count}
            for pid, count in workload.items()],
        "active_count": len(result), "due_soon_count": sum(1 for row in result if row["days_until_deadline"] is not None and 0 <= row["days_until_deadline"] <= 30)}


@router.post("", response_model=schemas.AuditEngagementOut,
             status_code=status.HTTP_201_CREATED)
def create_audit_engagement(
    payload: schemas.AuditEngagementCreate,
    request: Request,
    actor: models.User = Depends(require_role(Role.MANAGER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Create one engagement type for a client and financial year.

    The firm id is always stamped by TenantSession. Engagement owners must be
    active Partners from that same firm; caller-supplied ids are looked up
    through the tenant-scoped session.
    """
    client = tenant.get(models.Client, payload.client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="Client not found")

    partner = tenant.get(models.User, payload.engagement_partner_id)
    if (partner is None or not partner.is_active
            or partner.role != models.UserRole.PARTNER):
        raise HTTPException(
            status_code=422,
            detail="Engagement partner must be an active Partner in this firm.",
        )

    existing = tenant.query(models.AuditEngagement).filter(
        models.AuditEngagement.client_id == client.id,
        models.AuditEngagement.financial_year == payload.financial_year,
        models.AuditEngagement.engagement_type == payload.engagement_type,
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="An engagement of this type already exists for this client and financial year.",
        )

    engagement = models.AuditEngagement(
        client_id=client.id,
        financial_year=payload.financial_year,
        engagement_type=payload.engagement_type,
        engagement_partner_id=partner.id,
        created_by=actor.id,
        reporting_deadline=payload.reporting_deadline,
    )
    tenant.add(engagement)
    tenant.flush()

    audit.record(
        tenant.db,
        firm_id=tenant.firm_id,
        user_id=actor.id,
        action=AuditAction.AUDIT_ENGAGEMENT_CREATED,
        entity_type="AuditEngagement",
        entity_id=engagement.id,
        metadata={
            "client_id": client.id,
            "financial_year": engagement.financial_year,
            "engagement_type": engagement.engagement_type.value,
            "engagement_partner_id": partner.id,
            "status": engagement.status.value,
        },
        ip_address=client_ip(request),
        user_agent=user_agent(request),
    )
    try:
        tenant.commit()
    except IntegrityError as exc:
        tenant.rollback()
        # The unique constraint is the concurrency-safe duplicate guard.
        raise HTTPException(
            status_code=409,
            detail="An engagement of this type already exists for this client and financial year.",
        ) from exc
    tenant.refresh(engagement)
    return engagement


@router.get("", response_model=list[schemas.AuditEngagementOut])
def list_audit_engagements(
    client_id: Optional[str] = Query(None),
    financial_year: Optional[str] = Query(None, pattern=r"^20\d{2}-\d{2}$"),
    engagement_type: Optional[models.AuditEngagementType] = Query(None),
    engagement_status: Optional[models.AuditEngagementStatus] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    query = tenant.query(models.AuditEngagement)
    if client_id:
        query = query.filter(models.AuditEngagement.client_id == client_id)
    if financial_year:
        query = query.filter(models.AuditEngagement.financial_year == financial_year)
    if engagement_type:
        query = query.filter(models.AuditEngagement.engagement_type == engagement_type)
    if engagement_status:
        query = query.filter(models.AuditEngagement.status == engagement_status)
    return query.order_by(
        models.AuditEngagement.financial_year.desc(),
        models.AuditEngagement.created_at.desc(),
    ).limit(limit).all()


@router.get("/{engagement_id}", response_model=schemas.AuditEngagementOut)
def get_audit_engagement(
    engagement_id: str,
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    return tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
