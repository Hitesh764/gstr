"""Versioned firm audit policy with a separate partner approval action."""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/audit-policy", tags=["audit-policy"])


def _policy_metadata(policy: models.FirmAuditPolicy) -> dict:
    return {
        "policy_id": policy.id,
        "version": policy.version,
        "status": policy.status.value,
        "effective_from": policy.effective_from.isoformat(),
        "effective_to": policy.effective_to.isoformat() if policy.effective_to else None,
        "materiality_default_basis": policy.materiality_default_basis.value,
        "materiality_percentages": {
            str(key.value if hasattr(key, "value") else key): str(value)
            for key, value in policy.materiality_percentages.items()
        },
        "variance_threshold_pct": str(policy.variance_threshold_pct),
        "variance_materiality_multiplier": str(policy.variance_materiality_multiplier),
        "ratio_movement_threshold_pct": str(policy.ratio_movement_threshold_pct),
    }


@router.post("", response_model=schemas.FirmAuditPolicyOut,
             status_code=status.HTTP_201_CREATED)
def draft_policy(
    payload: schemas.FirmAuditPolicyCreate,
    request: Request,
    actor: models.User = Depends(require_role(Role.MANAGER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Save a policy version as a draft. It has no effect until approved."""
    latest = tenant.query(models.FirmAuditPolicy).order_by(
        models.FirmAuditPolicy.version.desc()
    ).first()
    version = latest.version + 1 if latest else 1
    percentages = {
        key.value if hasattr(key, "value") else str(key): str(value)
        for key, value in payload.materiality_percentages.items()
    }
    policy = models.FirmAuditPolicy(
        version=version,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
        materiality_default_basis=payload.materiality_default_basis,
        materiality_percentages=percentages,
        variance_threshold_pct=payload.variance_threshold_pct,
        variance_materiality_multiplier=payload.variance_materiality_multiplier,
        ratio_movement_threshold_pct=payload.ratio_movement_threshold_pct,
        created_by=actor.id,
    )
    tenant.add(policy)
    tenant.flush()
    audit.record(
        tenant.db,
        firm_id=tenant.firm_id,
        user_id=actor.id,
        action=AuditAction.AUDIT_POLICY_DRAFTED,
        entity_type="FirmAuditPolicy",
        entity_id=policy.id,
        metadata=_policy_metadata(policy),
        ip_address=client_ip(request),
        user_agent=user_agent(request),
    )
    try:
        tenant.commit()
    except IntegrityError as exc:
        tenant.rollback()
        raise HTTPException(status_code=409, detail="Could not create policy version.") from exc
    tenant.refresh(policy)
    return policy


@router.get("", response_model=list[schemas.FirmAuditPolicyOut])
def list_policies(
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    return tenant.query(models.FirmAuditPolicy).order_by(
        models.FirmAuditPolicy.version.desc()
    ).all()


@router.get("/active", response_model=schemas.FirmAuditPolicyOut)
def active_policy(
    as_of: date | None = None,
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    on_date = as_of or date.today()
    policy = tenant.query(models.FirmAuditPolicy).filter(
        models.FirmAuditPolicy.status == models.AuditPolicyStatus.APPROVED,
        models.FirmAuditPolicy.effective_from <= on_date,
        (models.FirmAuditPolicy.effective_to.is_(None)
         | (models.FirmAuditPolicy.effective_to >= on_date)),
    ).order_by(models.FirmAuditPolicy.version.desc()).first()
    if policy is None:
        raise HTTPException(status_code=404, detail="No approved audit policy is effective on this date.")
    return policy


@router.post("/{policy_id}/approve", response_model=schemas.FirmAuditPolicyOut)
def approve_policy(
    policy_id: str,
    request: Request,
    actor: models.User = Depends(require_role(Role.PARTNER)),
    tenant: TenantSession = Depends(get_tenant),
):
    policy = tenant.get_or_404(models.FirmAuditPolicy, policy_id, "Audit policy not found")
    if policy.status != models.AuditPolicyStatus.DRAFT:
        raise HTTPException(status_code=409, detail="Only a draft audit policy can be approved.")
    if policy.created_by == actor.id:
        raise HTTPException(status_code=409, detail="You cannot approve your own audit policy draft.")

    approved = tenant.query(models.FirmAuditPolicy).filter(
        models.FirmAuditPolicy.status == models.AuditPolicyStatus.APPROVED,
        models.FirmAuditPolicy.id != policy.id,
    ).all()
    policy_end = policy.effective_to
    closed_previous: list[dict] = []
    for prior in approved:
        overlaps = (
            (prior.effective_to is None or prior.effective_to >= policy.effective_from)
            and (policy_end is None or prior.effective_from <= policy_end)
        )
        if not overlaps:
            continue
        # A new later version may succeed an open-ended prior version. Close
        # that prior date range rather than allowing ambiguous active policy.
        if prior.effective_from < policy.effective_from and prior.effective_to is None:
            prior.effective_to = policy.effective_from - timedelta(days=1)
            closed_previous.append({
                "policy_id": prior.id,
                "effective_to": prior.effective_to.isoformat(),
            })
            continue
        raise HTTPException(
            status_code=409,
            detail="The policy effective dates overlap another approved policy.",
        )

    policy.status = models.AuditPolicyStatus.APPROVED
    policy.approved_by = actor.id
    policy.approved_at = datetime.utcnow()
    audit.record(
        tenant.db,
        firm_id=tenant.firm_id,
        user_id=actor.id,
        action=AuditAction.AUDIT_POLICY_APPROVED,
        entity_type="FirmAuditPolicy",
        entity_id=policy.id,
        metadata={**_policy_metadata(policy), "closed_previous_versions": closed_previous},
        ip_address=client_ip(request),
        user_agent=user_agent(request),
    )
    tenant.commit()
    tenant.refresh(policy)
    return policy
