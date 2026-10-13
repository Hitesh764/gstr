"""
Client and GSTIN management.

Every route here takes a `TenantSession` rather than a raw database
session, so `firm_id` is applied from the authenticated user and a client
belonging to another firm is simply not visible — it 404s rather than
403s, which avoids confirming that the id exists at all.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import (
    Role, assert_client_visible, client_ip, get_current_user, get_tenant,
    require_role, user_agent,
)
from app.services import consent as consent_svc
from app.tenancy import TenantSession
from app.validation import normalize_gstin, state_from_gstin, validate_gstin

router = APIRouter(prefix="/api/clients", tags=["clients"])


def _gstin_out(g: models.ClientGSTIN) -> schemas.ClientGSTINOut:
    data = schemas.ClientGSTINOut.model_validate(g)
    state = consent_svc.consent_state(g)
    data.gsp_state = state.value
    data.gsp_provider = g.gsp_provider
    data.has_active_consent = state in (consent_svc.ConsentState.ACTIVE,
                                        consent_svc.ConsentState.EXPIRING)
    return data


def _client_out(c: models.Client) -> schemas.ClientOut:
    out = schemas.ClientOut.model_validate(c)
    out.gstins = [_gstin_out(g) for g in c.gstins if g.deleted_at is None]
    return out


@router.get("", response_model=list[schemas.ClientOut])
def list_clients(
    status_filter: Optional[models.ClientStatus] = Query(None, alias="status"),
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    q = tenant.query(models.Client)
    if status_filter:
        q = q.filter(models.Client.status == status_filter)

    # A client viewer sees only the single client they are scoped to.
    if user.role == models.UserRole.CLIENT_VIEWER:
        if not user.scoped_client_id:
            return []
        q = q.filter(models.Client.id == user.scoped_client_id)

    return [_client_out(c) for c in q.order_by(models.Client.name.asc()).all()]


@router.post("", response_model=schemas.ClientOut, status_code=status.HTTP_201_CREATED)
def create_client(
    payload: schemas.ClientCreate,
    request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    if payload.assigned_associate_id:
        tenant.get_or_404(models.User, payload.assigned_associate_id,
                          "Assigned associate not found in this firm")

    client = models.Client(
        name=payload.name.strip(),
        pan=payload.pan,
        entity_type=payload.entity_type,
        engagement_type=payload.engagement_type,
        status=payload.status,
        contact_name=payload.contact_name,
        contact_email=payload.contact_email,
        contact_phone=payload.contact_phone,
        assigned_associate_id=payload.assigned_associate_id,
        reconciliation_frequency=payload.reconciliation_frequency,
    )
    tenant.add(client)
    tenant.flush()

    for i, g in enumerate(payload.gstins):
        _attach_gstin(tenant, client, g.gstin, is_primary=g.is_primary or i == 0)

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.CLIENT_CREATED, entity_type="Client", entity_id=client.id,
        metadata={"name": client.name, "gstins": [g.gstin for g in payload.gstins]},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    tenant.refresh(client)
    return _client_out(client)


def _attach_gstin(
    tenant: TenantSession, client: models.Client, gstin: str, is_primary: bool = False,
) -> models.ClientGSTIN:
    gstin = normalize_gstin(gstin)
    ok, msg = validate_gstin(gstin)
    if not ok:
        raise HTTPException(status_code=422, detail=msg)

    # Unique per firm: the same GSTIN cannot be registered against two
    # clients within one firm, which would make reconciliation ambiguous.
    clash = (
        tenant.query(models.ClientGSTIN)
        .filter(models.ClientGSTIN.gstin == gstin)
        .first()
    )
    if clash:
        raise HTTPException(
            status_code=409,
            detail=f"GSTIN {gstin} is already registered to another client in this firm.",
        )

    code, name = state_from_gstin(gstin)
    row = models.ClientGSTIN(
        client_id=client.id, gstin=gstin, state_code=code,
        state_name=name, is_primary=is_primary,
    )
    tenant.add(row)
    return row


@router.get("/{client_id}", response_model=schemas.ClientOut)
def get_client(
    client_id: str,
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    client = tenant.get_or_404(models.Client, client_id, "Client not found")
    assert_client_visible(user, client_id)
    return _client_out(client)


@router.patch("/{client_id}", response_model=schemas.ClientOut)
def update_client(
    client_id: str,
    payload: schemas.ClientUpdate,
    request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    client = tenant.get_or_404(models.Client, client_id, "Client not found")
    changes = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is None:
            continue
        if field == "assigned_associate_id":
            tenant.get_or_404(models.User, value, "Assigned associate not found in this firm")
        old = getattr(client, field)
        if old != value:
            changes[field] = {"from": str(old), "to": str(value)}
            setattr(client, field, value)

    if changes:
        audit.record(
            tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.CLIENT_UPDATED, entity_type="Client", entity_id=client.id,
            metadata={"changes": changes},
            ip_address=client_ip(request), user_agent=user_agent(request),
        )
    tenant.commit()
    tenant.refresh(client)
    return _client_out(client)


@router.delete("/{client_id}", response_model=schemas.MessageOut)
def delete_client(
    client_id: str,
    request: Request,
    retention_days: int = Query(2555, description="Default ~7 years, the usual record-keeping period"),
    actor: models.User = Depends(require_role(Role.MANAGER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Soft delete. The record is flagged and retained, never destroyed."""
    client = tenant.get_or_404(models.Client, client_id, "Client not found")
    tenant.soft_delete(client, retention_days=retention_days)
    for g in client.gstins:
        tenant.soft_delete(g, retention_days=retention_days)

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.CLIENT_DELETED, entity_type="Client", entity_id=client.id,
        metadata={"name": client.name, "retention_days": retention_days},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    return schemas.MessageOut(
        message=f"Client archived. Data is retained for {retention_days} days."
    )


# ---------------------------------------------------------------------------
# GSTINs
# ---------------------------------------------------------------------------

@router.post("/{client_id}/gstins", response_model=schemas.ClientGSTINOut,
             status_code=status.HTTP_201_CREATED)
def add_gstin(
    client_id: str,
    payload: schemas.ClientGSTINIn,
    request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    client = tenant.get_or_404(models.Client, client_id, "Client not found")
    row = _attach_gstin(tenant, client, payload.gstin, is_primary=payload.is_primary)
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.GSTIN_ADDED, entity_type="ClientGSTIN", entity_id=row.id,
        metadata={"client_id": client_id, "gstin": row.gstin},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    tenant.refresh(row)
    return _gstin_out(row)


@router.get("/{client_id}/gstins", response_model=list[schemas.ClientGSTINOut])
def list_gstins(
    client_id: str,
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.Client, client_id, "Client not found")
    assert_client_visible(user, client_id)
    rows = (
        tenant.query(models.ClientGSTIN)
        .filter(models.ClientGSTIN.client_id == client_id)
        .order_by(models.ClientGSTIN.is_primary.desc(), models.ClientGSTIN.gstin.asc())
        .all()
    )
    return [_gstin_out(g) for g in rows]


@router.delete("/{client_id}/gstins/{gstin_id}", response_model=schemas.MessageOut)
def remove_gstin(
    client_id: str,
    gstin_id: str,
    request: Request,
    actor: models.User = Depends(require_role(Role.MANAGER)),
    tenant: TenantSession = Depends(get_tenant),
):
    row = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    if row.client_id != client_id:
        raise HTTPException(status_code=404, detail="GSTIN not found for this client")
    tenant.soft_delete(row)
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.GSTIN_REMOVED, entity_type="ClientGSTIN", entity_id=row.id,
        metadata={"gstin": row.gstin},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    return schemas.MessageOut(message=f"GSTIN {row.gstin} removed from this client.")


@router.get("/{client_id}/periods")
def list_periods(
    client_id: str,
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    """Every period with purchase register or GSTR-2B data, per GSTIN."""
    tenant.get_or_404(models.Client, client_id, "Client not found")
    assert_client_visible(user, client_id)

    gstin_ids = [
        g.id for g in tenant.query(models.ClientGSTIN)
        .filter(models.ClientGSTIN.client_id == client_id).all()
    ]
    if not gstin_ids:
        return []

    uploads = (
        tenant.query(models.PurchaseRegisterUpload)
        .filter(models.PurchaseRegisterUpload.client_gstin_id.in_(gstin_ids))
        .all()
    )
    fetches = (
        tenant.query(models.GSTR2BFetch)
        .filter(models.GSTR2BFetch.client_gstin_id.in_(gstin_ids))
        .all()
    )

    by_period: dict[str, dict] = {}
    for u in uploads:
        entry = by_period.setdefault(u.period, {"period": u.period, "gstin_ids": set(),
                                                "has_purchase_register": False,
                                                "has_gstr2b": False})
        entry["gstin_ids"].add(u.client_gstin_id)
        entry["has_purchase_register"] = True
    for f in fetches:
        entry = by_period.setdefault(f.period, {"period": f.period, "gstin_ids": set(),
                                                "has_purchase_register": False,
                                                "has_gstr2b": False})
        entry["gstin_ids"].add(f.client_gstin_id)
        entry["has_gstr2b"] = True

    return [
        {**v, "gstin_ids": sorted(v["gstin_ids"])}
        for v in sorted(by_period.values(), key=lambda x: x["period"], reverse=True)
    ]
