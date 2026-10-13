"""
Notification centre and per-user delivery preferences.

Notifications belong to a person, not a firm, so every route here filters by
the authenticated user's own id as well as by tenant. A manager cannot read
a colleague's notifications, which is why these do not simply use the
firm-wide scoping and stop there.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app import models
from app.deps import get_current_user, get_tenant
from app.notifications import EVENT_CATALOGUE, resolve_preference, unread_count
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    type: str
    title: str
    message: Optional[str] = None
    link: Optional[str] = None
    read_status: bool
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread: int


class PreferenceOut(BaseModel):
    event_type: str
    label: str
    description: str
    in_app: bool
    email: bool


class PreferenceIn(BaseModel):
    event_type: str
    in_app: bool
    email: bool


class PreferencesUpdateIn(BaseModel):
    preferences: list[PreferenceIn] = Field(..., min_length=1, max_length=100)


class MessageOut(BaseModel):
    message: str


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(False),
    limit: int = Query(50, le=200),
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    q = tenant.query(models.Notification).filter(
        models.Notification.user_id == user.id
    )
    if unread_only:
        q = q.filter(models.Notification.read_status.is_(False))
    return (
        q.order_by(models.Notification.created_at.desc()).limit(limit).all()
    )


@router.get("/unread-count", response_model=UnreadCountOut)
def get_unread_count(
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    return UnreadCountOut(unread=unread_count(tenant.db, user.id))


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: str,
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    row = tenant.get(models.Notification, notification_id)
    # Belonging to the right firm is not enough: it must be this user's own.
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.read_status = True
    tenant.commit()
    tenant.refresh(row)
    return row


@router.post("/read-all", response_model=MessageOut)
def mark_all_read(
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    rows = (
        tenant.query(models.Notification)
        .filter(models.Notification.user_id == user.id,
                models.Notification.read_status.is_(False))
        .all()
    )
    for row in rows:
        row.read_status = True
    tenant.commit()
    return MessageOut(message=f"Marked {len(rows)} notification(s) as read.")


@router.get("/preferences", response_model=list[PreferenceOut])
def get_preferences(
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    """Every configurable event, with this user's effective setting.

    Built from the catalogue rather than from stored rows, so an event the
    user has never touched still appears, showing its default.
    """
    out = []
    for event_type, spec in EVENT_CATALOGUE.items():
        in_app, email = resolve_preference(tenant.db, user.id, event_type)
        out.append(PreferenceOut(
            event_type=event_type, label=spec.label,
            description=spec.description, in_app=in_app, email=email,
        ))
    return out


@router.put("/preferences", response_model=list[PreferenceOut])
def update_preferences(
    payload: PreferencesUpdateIn,
    user: models.User = Depends(get_current_user),
    tenant: TenantSession = Depends(get_tenant),
):
    unknown = [p.event_type for p in payload.preferences
               if p.event_type not in EVENT_CATALOGUE]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown notification event(s): {', '.join(unknown)}",
        )

    for item in payload.preferences:
        row = (
            tenant.query(models.NotificationPreference)
            .filter(models.NotificationPreference.user_id == user.id,
                    models.NotificationPreference.event_type == item.event_type)
            .first()
        )
        if row is None:
            row = models.NotificationPreference(
                user_id=user.id, event_type=item.event_type,
                in_app=item.in_app, email=item.email,
            )
            tenant.add(row)
        else:
            row.in_app = item.in_app
            row.email = item.email

    tenant.commit()
    return get_preferences(user=user, tenant=tenant)
