"""
Exception resolution workflow: assignment, resolution, approval, comments.

The approval chain is the point of this module:

    OPEN -> (associate resolves) -> PENDING_APPROVAL -> (manager approves)
                                                     -> APPROVED
                                                     -> REWORK -> back to the associate

Only APPROVED and AUTO_RESOLVED items count toward a clean reconciliation.
An associate cannot approve their own work: `require_role(MANAGER)` guards
the approval endpoint, and a self-approval by someone who holds both roles
is rejected explicitly, because separation of duties is the entire reason
the workflow exists.
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import joinedload

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_current_user, get_tenant, require_role, user_agent
from app.notifications import NotificationType, notify, notify_approvers
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/exceptions", tags=["exceptions"])


class BulkResolveIn(BaseModel):
    exception_ids: list[str] = Field(..., min_length=1, max_length=500)
    resolution_notes: str = Field(..., min_length=1)


class BulkAssignIn(BaseModel):
    exception_ids: list[str] = Field(..., min_length=1, max_length=500)
    assigned_to: Optional[str] = None


class AssignIn(BaseModel):
    assigned_to: Optional[str] = None


def _days_until(deadline: Optional[date]) -> Optional[int]:
    return (deadline - date.today()).days if deadline else None


def _urgency(days: Optional[int]) -> str:
    """Visual escalation for an approaching ITC lapse deadline."""
    if days is None:
        return "none"
    if days < 0:
        return "lapsed"
    if days <= 30:
        return "critical"
    if days <= 90:
        return "warning"
    return "ok"


def _exception_payload(exc: models.ReconciliationException) -> dict:
    days = _days_until(exc.itc_lapse_deadline)
    return {
        "id": exc.id,
        "status": exc.status.value if hasattr(exc.status, "value") else exc.status,
        "client_gstin_id": exc.client_gstin_id,
        "period": exc.period,
        "assigned_to": exc.assigned_to,
        "resolution_notes": exc.resolution_notes,
        "resolved_by": exc.resolved_by,
        "resolved_at": exc.resolved_at,
        "approved_by": exc.approved_by,
        "approved_at": exc.approved_at,
        "rework_reason": exc.rework_reason,
        "itc_lapse_deadline": exc.itc_lapse_deadline,
        "days_until_lapse": days,
        "urgency": _urgency(days),
        "created_at": exc.created_at,
    }


@router.get("")
def list_exceptions(
    client_gstin_id: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    status_filter: Optional[models.ExceptionStatus] = Query(None, alias="status"),
    assigned_to: Optional[str] = Query(None),
    urgency: Optional[str] = Query(None, description="lapsed | critical | warning | ok"),
    limit: int = Query(200, le=1000),
    _: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Firm-wide exception queue, filterable."""
    q = tenant.query(models.ReconciliationException)
    if client_gstin_id:
        q = q.filter(models.ReconciliationException.client_gstin_id == client_gstin_id)
    if period:
        q = q.filter(models.ReconciliationException.period == period)
    if status_filter:
        q = q.filter(models.ReconciliationException.status == status_filter)
    if assigned_to:
        q = q.filter(models.ReconciliationException.assigned_to == assigned_to)

    rows = q.order_by(
        models.ReconciliationException.itc_lapse_deadline.asc().nullslast(),
        models.ReconciliationException.created_at.desc(),
    ).limit(limit).all()

    payload = [_exception_payload(e) for e in rows]
    if urgency:
        payload = [p for p in payload if p["urgency"] == urgency]
    return payload


@router.get("/{exception_id}")
def get_exception(
    exception_id: str,
    _: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    exc = tenant.get_or_404(models.ReconciliationException, exception_id, "Exception not found")
    payload = _exception_payload(exc)

    result = tenant.get(models.ReconciliationResult, exc.reconciliation_result_id)
    if result:
        payload["result"] = schemas.ReconciliationResultOut.model_validate(result).model_dump()

    payload["comments"] = [
        schemas.ExceptionCommentOut.model_validate(c).model_dump()
        for c in tenant.query(models.ExceptionComment)
        .filter(models.ExceptionComment.exception_id == exception_id)
        .order_by(models.ExceptionComment.created_at.asc()).all()
    ]
    return payload


@router.patch("/{exception_id}/assign")
def assign_exception(
    exception_id: str,
    payload: AssignIn,
    request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    exc = tenant.get_or_404(models.ReconciliationException, exception_id, "Exception not found")
    if payload.assigned_to:
        tenant.get_or_404(models.User, payload.assigned_to, "User not found in this firm")
    exc.assigned_to = payload.assigned_to
    if exc.status == models.ExceptionStatus.OPEN and payload.assigned_to:
        exc.status = models.ExceptionStatus.IN_PROGRESS

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.EXCEPTION_ASSIGNED,
        entity_type="ReconciliationException", entity_id=exc.id,
        metadata={"assigned_to": payload.assigned_to},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    # Telling someone they have been given work is the whole point of the
    # action, so notify the assignee (but not if they assigned it to themselves).
    if payload.assigned_to and payload.assigned_to != actor.id:
        notify(
            tenant.db, user_id=payload.assigned_to, firm_id=tenant.firm_id,
            event_type=NotificationType.EXCEPTION_ASSIGNED,
            title="An exception was assigned to you",
            message=f"{actor.name} assigned you a reconciliation exception "
                    f"for period {exc.period}.",
            link=f"/exceptions?status=OPEN",
            entity_type="ReconciliationException", entity_id=exc.id,
        )
    tenant.commit()
    tenant.refresh(exc)
    return _exception_payload(exc)


@router.patch("/{exception_id}/resolve")
def resolve_exception(
    exception_id: str,
    payload: schemas.ResolveExceptionIn,
    request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Propose a resolution. Moves the item to PENDING_APPROVAL, not to
    APPROVED: an associate's resolution is a proposal until a manager
    signs it off."""
    exc = tenant.get_or_404(models.ReconciliationException, exception_id, "Exception not found")
    if exc.status == models.ExceptionStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail="This exception has already been approved and is locked.",
        )

    exc.resolution_notes = payload.resolution_notes
    exc.resolved_by = actor.id
    exc.resolved_at = datetime.utcnow()
    exc.status = models.ExceptionStatus.PENDING_APPROVAL
    exc.rework_reason = None

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.EXCEPTION_RESOLVED,
        entity_type="ReconciliationException", entity_id=exc.id,
        metadata={"notes": payload.resolution_notes[:500]},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    # The resolver cannot approve their own work, so the item is dead in the
    # water until someone else looks. Tell the people who can.
    notify_approvers(
        tenant.db, firm_id=tenant.firm_id,
        event_type=NotificationType.EXCEPTION_NEEDS_APPROVAL,
        title="A resolution needs your approval",
        message=f"{actor.name} resolved an exception for period {exc.period} "
                f"and it is waiting for sign-off.",
        link="/exceptions?status=PENDING_APPROVAL",
        entity_type="ReconciliationException", entity_id=exc.id,
        exclude_user_id=actor.id,
    )
    tenant.commit()
    tenant.refresh(exc)
    return _exception_payload(exc)


@router.patch("/{exception_id}/approve")
def approve_exception(
    exception_id: str,
    payload: schemas.ApproveExceptionIn,
    request: Request,
    actor: models.User = Depends(require_role(Role.MANAGER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Approve or send back for rework. Manager and above."""
    exc = tenant.get_or_404(models.ReconciliationException, exception_id, "Exception not found")

    if exc.status != models.ExceptionStatus.PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"Only exceptions awaiting approval can be actioned. "
                   f"This one is {exc.status.value}.",
        )

    # Separation of duties: the person who proposed the resolution cannot
    # also sign it off, even if their role would otherwise permit it.
    if exc.resolved_by == actor.id:
        raise HTTPException(
            status_code=403,
            detail="You resolved this exception, so it must be approved by someone else.",
        )

    if payload.approved:
        exc.status = models.ExceptionStatus.APPROVED
        exc.approved_by = actor.id
        exc.approved_at = datetime.utcnow()
        exc.rework_reason = None
        action = AuditAction.EXCEPTION_APPROVED
    else:
        if not payload.rework_reason:
            raise HTTPException(
                status_code=422,
                detail="Give a reason when sending an exception back for rework.",
            )
        exc.status = models.ExceptionStatus.REWORK
        exc.rework_reason = payload.rework_reason
        exc.approved_by = None
        exc.approved_at = None
        action = AuditAction.EXCEPTION_REWORK

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id, action=action,
        entity_type="ReconciliationException", entity_id=exc.id,
        metadata={"approved": payload.approved, "rework_reason": payload.rework_reason},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    # Close the loop with whoever proposed the resolution.
    if exc.resolved_by and exc.resolved_by != actor.id:
        if payload.approved:
            notify(
                tenant.db, user_id=exc.resolved_by, firm_id=tenant.firm_id,
                event_type=NotificationType.EXCEPTION_APPROVED,
                title="Your resolution was approved",
                message=f"{actor.name} approved your resolution for period {exc.period}.",
                link="/exceptions?status=APPROVED",
                entity_type="ReconciliationException", entity_id=exc.id,
            )
        else:
            notify(
                tenant.db, user_id=exc.resolved_by, firm_id=tenant.firm_id,
                event_type=NotificationType.EXCEPTION_REWORK,
                title="A resolution was sent back to you",
                message=f"{actor.name} returned your resolution for period "
                        f"{exc.period}. Reason: {payload.rework_reason}",
                link="/exceptions?status=REWORK",
                entity_type="ReconciliationException", entity_id=exc.id,
                force=True,   # a second rework round is genuinely new information
            )
    tenant.commit()
    tenant.refresh(exc)
    return _exception_payload(exc)


@router.post("/bulk/resolve")
def bulk_resolve(
    payload: BulkResolveIn,
    request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Resolve many similar exceptions at once.

    Typically used for a batch of formatting mismatches from one vendor,
    where the same explanation applies to all of them.
    """
    rows = (
        tenant.query(models.ReconciliationException)
        .filter(models.ReconciliationException.id.in_(payload.exception_ids))
        .all()
    )
    found = {r.id for r in rows}
    missing = [i for i in payload.exception_ids if i not in found]

    updated, skipped = [], []
    now = datetime.utcnow()
    for exc in rows:
        if exc.status == models.ExceptionStatus.APPROVED:
            skipped.append({"id": exc.id, "reason": "already approved and locked"})
            continue
        exc.resolution_notes = payload.resolution_notes
        exc.resolved_by = actor.id
        exc.resolved_at = now
        exc.status = models.ExceptionStatus.PENDING_APPROVAL
        exc.rework_reason = None
        updated.append(exc.id)

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.EXCEPTION_RESOLVED, entity_type="ReconciliationException",
        entity_id=None,
        metadata={"bulk": True, "count": len(updated),
                  "notes": payload.resolution_notes[:500]},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    return {"resolved": len(updated), "ids": updated,
            "skipped": skipped, "not_found": missing}


@router.post("/bulk/assign")
def bulk_assign(
    payload: BulkAssignIn,
    request: Request,
    actor: models.User = Depends(require_role(Role.MANAGER)),
    tenant: TenantSession = Depends(get_tenant),
):
    if payload.assigned_to:
        tenant.get_or_404(models.User, payload.assigned_to, "User not found in this firm")

    rows = (
        tenant.query(models.ReconciliationException)
        .filter(models.ReconciliationException.id.in_(payload.exception_ids))
        .all()
    )
    for exc in rows:
        exc.assigned_to = payload.assigned_to
        if exc.status == models.ExceptionStatus.OPEN and payload.assigned_to:
            exc.status = models.ExceptionStatus.IN_PROGRESS

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.EXCEPTION_ASSIGNED, entity_type="ReconciliationException",
        entity_id=None,
        metadata={"bulk": True, "count": len(rows), "assigned_to": payload.assigned_to},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    return {"assigned": len(rows), "ids": [r.id for r in rows]}


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@router.post("/{exception_id}/comments", response_model=schemas.ExceptionCommentOut,
             status_code=status.HTTP_201_CREATED)
def add_comment(
    exception_id: str,
    payload: schemas.ExceptionCommentIn,
    request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.ReconciliationException, exception_id, "Exception not found")
    comment = models.ExceptionComment(
        exception_id=exception_id, user_id=actor.id, comment=payload.comment,
    )
    tenant.add(comment)
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.EXCEPTION_COMMENTED,
        entity_type="ReconciliationException", entity_id=exception_id,
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    exc = tenant.get(models.ReconciliationException, exception_id)
    if exc and exc.assigned_to and exc.assigned_to != actor.id:
        notify(
            tenant.db, user_id=exc.assigned_to, firm_id=tenant.firm_id,
            event_type=NotificationType.EXCEPTION_COMMENT,
            title="New comment on your exception",
            message=f"{actor.name}: {payload.comment[:200]}",
            link="/exceptions",
            entity_type="ReconciliationException", entity_id=exception_id,
            force=True,   # each comment is new information
        )
    tenant.commit()
    tenant.refresh(comment)
    return comment


@router.get("/{exception_id}/comments", response_model=list[schemas.ExceptionCommentOut])
def list_comments(
    exception_id: str,
    _: models.User = Depends(require_role(Role.CLIENT_VIEWER)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.ReconciliationException, exception_id, "Exception not found")
    return (
        tenant.query(models.ExceptionComment)
        .filter(models.ExceptionComment.exception_id == exception_id)
        .order_by(models.ExceptionComment.created_at.asc())
        .all()
    )
