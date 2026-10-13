"""
Notifications: in-app first, email second, both per-user configurable.

Design notes:

**In-app is the record; email is a copy.** Every notification is written to
the database regardless of preferences, unless the user has explicitly
turned that event off in-app. Email is a best-effort push on top. If the
mail provider fails, the user still sees the item when they next open the
app, so a delivery failure never loses the signal.

**Delivery is deduplicated by entity.** Scheduled jobs re-run, and a retry
after a partial failure would otherwise email someone twice about the same
GSTR-2B fetch. `notify` refuses to create a second unread notification for
the same (user, event, entity) triple within a suppression window.

**Email never blocks the request.** A send failure is logged and the
notification is still saved. A CA waiting on an upload should not see it
fail because a mail server was slow.

Defaults are deliberately conservative: things a person must act on default
to email as well as in-app; things that are merely informational default to
in-app only. Nobody should have to opt out of noise on day one.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.services import messaging

logger = logging.getLogger("gstr2b.notifications")

# A repeat notification about the same entity within this window is treated
# as a duplicate and suppressed.
DEDUPE_WINDOW = timedelta(hours=12)


class NotificationType:
    """Event vocabulary. Values are stored, so do not rename casually."""

    # Exception workflow
    EXCEPTION_ASSIGNED = "EXCEPTION_ASSIGNED"
    EXCEPTION_NEEDS_APPROVAL = "EXCEPTION_NEEDS_APPROVAL"
    EXCEPTION_APPROVED = "EXCEPTION_APPROVED"
    EXCEPTION_REWORK = "EXCEPTION_REWORK"
    EXCEPTION_COMMENT = "EXCEPTION_COMMENT"

    # Reconciliation and data
    RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"
    PURCHASE_REGISTER_IMPORTED = "PURCHASE_REGISTER_IMPORTED"

    # GSP integration (used from section 1 onward)
    GSTR2B_FETCH_COMPLETED = "GSTR2B_FETCH_COMPLETED"
    GSTR2B_FETCH_FAILED = "GSTR2B_FETCH_FAILED"
    GSP_CONSENT_EXPIRING = "GSP_CONSENT_EXPIRING"
    GSP_CONSENT_EXPIRED = "GSP_CONSENT_EXPIRED"

    # Notices (section 3)
    NOTICE_DEADLINE_APPROACHING = "NOTICE_DEADLINE_APPROACHING"
    NOTICE_NEEDS_APPROVAL = "NOTICE_NEEDS_APPROVAL"
    NOTICE_ANALYSED = "NOTICE_ANALYSED"
    NOTICE_ASSIGNED = "NOTICE_ASSIGNED"
    NOTICE_APPROVED = "NOTICE_APPROVED"
    NOTICE_SENT_BACK = "NOTICE_SENT_BACK"

    # Credit at risk
    ITC_LAPSE_APPROACHING = "ITC_LAPSE_APPROACHING"

    # Billing (section 5)
    PLAN_LIMIT_APPROACHING = "PLAN_LIMIT_APPROACHING"
    PAYMENT_FAILED = "PAYMENT_FAILED"


@dataclass(frozen=True)
class EventSpec:
    label: str            # shown on the preferences screen
    description: str
    default_in_app: bool = True
    default_email: bool = False


# One entry per event type. The preferences screen is generated from this,
# so adding an event here is all that is needed to make it configurable.
EVENT_CATALOGUE: dict[str, EventSpec] = {
    NotificationType.EXCEPTION_ASSIGNED: EventSpec(
        "Exception assigned to me",
        "Someone assigns you a reconciliation exception to work on.",
        default_email=True,
    ),
    NotificationType.EXCEPTION_NEEDS_APPROVAL: EventSpec(
        "Approval needed",
        "An associate has proposed a resolution that needs your sign-off.",
        default_email=True,
    ),
    NotificationType.EXCEPTION_APPROVED: EventSpec(
        "My resolution was approved",
        "A manager approved a resolution you wrote.",
    ),
    NotificationType.EXCEPTION_REWORK: EventSpec(
        "Sent back for rework",
        "A manager returned your resolution with a reason.",
        default_email=True,
    ),
    NotificationType.EXCEPTION_COMMENT: EventSpec(
        "Comment on an exception",
        "Someone commented on an exception assigned to you.",
    ),
    NotificationType.RECONCILIATION_COMPLETED: EventSpec(
        "Reconciliation finished",
        "A reconciliation run completed for a client you are assigned to.",
    ),
    NotificationType.PURCHASE_REGISTER_IMPORTED: EventSpec(
        "Purchase register imported",
        "A new purchase register version was imported for your client.",
    ),
    NotificationType.GSTR2B_FETCH_COMPLETED: EventSpec(
        "GSTR-2B received",
        "New GSTR-2B data arrived from the GST portal.",
        default_email=True,
    ),
    NotificationType.GSTR2B_FETCH_FAILED: EventSpec(
        "GSTR-2B fetch failed",
        "An automatic GSTR-2B fetch failed and needs attention.",
        default_email=True,
    ),
    NotificationType.GSP_CONSENT_EXPIRING: EventSpec(
        "GST portal access expiring",
        "A client's consent is close to expiry and must be renewed.",
        default_email=True,
    ),
    NotificationType.GSP_CONSENT_EXPIRED: EventSpec(
        "GST portal access expired",
        "Automatic fetching has stopped for a client until consent is renewed.",
        default_email=True,
    ),
    NotificationType.NOTICE_DEADLINE_APPROACHING: EventSpec(
        "Notice deadline approaching",
        "A GST notice response is due in 7, 3 or 1 day.",
        default_email=True,
    ),
    NotificationType.NOTICE_NEEDS_APPROVAL: EventSpec(
        "Notice response needs approval",
        "A drafted notice response is waiting for partner sign-off.",
        default_email=True,
    ),
    NotificationType.NOTICE_ANALYSED: EventSpec(
        "Notice analysis ready",
        "An uploaded notice has been read and classified.",
    ),
    NotificationType.NOTICE_ASSIGNED: EventSpec(
        "Notice assigned to me",
        "Someone assigns you a GST notice to handle.",
        default_email=True,
    ),
    NotificationType.NOTICE_APPROVED: EventSpec(
        "Notice response approved",
        "A partner approved a notice response you drafted or submitted.",
        default_email=True,
    ),
    NotificationType.NOTICE_SENT_BACK: EventSpec(
        "Notice response sent back",
        "A partner returned a notice response to you for changes.",
        default_email=True,
    ),
    NotificationType.ITC_LAPSE_APPROACHING: EventSpec(
        "Input tax credit about to lapse",
        "Unresolved credit is nearing its statutory claim deadline.",
        default_email=True,
    ),
    NotificationType.PLAN_LIMIT_APPROACHING: EventSpec(
        "Approaching plan limit",
        "Your firm is close to the client limit on its current plan.",
        default_email=True,
    ),
    NotificationType.PAYMENT_FAILED: EventSpec(
        "Payment failed",
        "A subscription payment did not go through.",
        default_email=True,
    ),
}


def resolve_preference(
    db: Session, user_id: str, event_type: str,
) -> tuple[bool, bool]:
    """Returns (in_app, email) for this user and event.

    Falls back to the catalogue default when the user has never expressed a
    preference, so a newly added event type works without backfilling rows.
    """
    spec = EVENT_CATALOGUE.get(event_type)
    default_in_app = spec.default_in_app if spec else True
    default_email = spec.default_email if spec else False

    pref = (
        db.query(models.NotificationPreference)
        .filter(models.NotificationPreference.user_id == user_id,
                models.NotificationPreference.event_type == event_type)
        .first()
    )
    if pref is None:
        return default_in_app, default_email
    return pref.in_app, pref.email


def _is_duplicate(
    db: Session, user_id: str, event_type: str,
    entity_type: Optional[str], entity_id: Optional[str],
    once: bool = False,
) -> bool:
    """Has this person already been told about this?

    By default a repeat within DEDUPE_WINDOW is a duplicate, but only while the
    earlier one is unread: reading it means they have seen it, and a fresh
    occurrence later is genuinely new.

    With `once`, ANY earlier notification for the same entity counts, however
    old and whether or not it was read. That suits events that happen once
    per cause, such as "this consent expired", where the entity id embeds the
    cause so a later, different expiry still notifies.
    """
    if not entity_id:
        return False
    q = db.query(models.Notification).filter(
        models.Notification.user_id == user_id,
        models.Notification.type == event_type,
        models.Notification.entity_type == entity_type,
        models.Notification.entity_id == entity_id,
    )
    if not once:
        q = q.filter(models.Notification.read_status.is_(False),
                     models.Notification.created_at >= datetime.utcnow() - DEDUPE_WINDOW)
    return q.first() is not None


def notify(
    db: Session,
    *,
    user_id: str,
    firm_id: str,
    event_type: str,
    title: str,
    message: Optional[str] = None,
    link: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    force: bool = False,
    once: bool = False,
) -> Optional[models.Notification]:
    """Create one notification. The caller commits.

    Returns the row, or None when it was suppressed as a duplicate or the
    user has turned this event off entirely. See `_is_duplicate` for `once`.
    """
    if not user_id or not firm_id:
        return None

    in_app, send_email = resolve_preference(db, user_id, event_type)
    if not in_app and not send_email:
        return None
    if not force and _is_duplicate(db, user_id, event_type, entity_type, entity_id, once=once):
        logger.debug("Suppressed duplicate %s for user %s on %s",
                     event_type, user_id, entity_id)
        return None

    row = models.Notification(
        firm_id=firm_id, user_id=user_id, type=event_type,
        title=title, message=message, link=link,
        entity_type=entity_type, entity_id=entity_id,
        read_status=not in_app,   # email-only: do not clutter the bell
    )
    db.add(row)
    db.flush()

    if send_email:
        _send_email(db, row)
    return row


def _send_email(db: Session, row: models.Notification) -> None:
    """Best-effort email. Never raises into the caller's request."""
    user = db.get(models.User, row.user_id)
    if user is None or not user.email or not user.is_active:
        return

    link = row.link or ""
    url = f"{settings.app_base_url}{link}" if link.startswith("/") else link
    body = (row.message or row.title)
    if url:
        body = f"{body}\n\nOpen it here:\n{url}"
    body += "\n\nTo change which emails you receive, open Notification settings in the app."

    try:
        result = messaging.get_email_provider().send(
            messaging.EmailMessage(
                to=user.email,
                subject=row.title,
                body=body,
                tags={"type": row.type},
            )
        )
        if result.ok:
            row.emailed_at = datetime.utcnow()
        else:
            logger.warning("Email for notification %s was not sent: %s",
                           row.id, result.error)
    except Exception as exc:  # a mail outage must not fail the request
        logger.warning("Email for notification %s raised: %s", row.id, exc)


def notify_many(
    db: Session, *, user_ids: Iterable[str], firm_id: str, event_type: str,
    title: str, message: Optional[str] = None, link: Optional[str] = None,
    entity_type: Optional[str] = None, entity_id: Optional[str] = None,
) -> int:
    """Notify several people about one thing. Returns how many were created."""
    seen: set[str] = set()
    created = 0
    for user_id in user_ids:
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        if notify(db, user_id=user_id, firm_id=firm_id, event_type=event_type,
                  title=title, message=message, link=link,
                  entity_type=entity_type, entity_id=entity_id) is not None:
            created += 1
    return created


def notify_approvers(
    db: Session, *, firm_id: str, event_type: str, title: str,
    message: Optional[str] = None, link: Optional[str] = None,
    entity_type: Optional[str] = None, entity_id: Optional[str] = None,
    exclude_user_id: Optional[str] = None,
) -> int:
    """Notify everyone in the firm who can approve, i.e. Manager and above.

    `exclude_user_id` leaves out the person who raised the item, since they
    cannot approve their own work and do not need telling about it.
    """
    approver_roles = (models.UserRole.MANAGER, models.UserRole.PARTNER,
                      models.UserRole.OWNER)
    rows = (
        db.query(models.User)
        .filter(models.User.firm_id == firm_id,
                models.User.role.in_(approver_roles),
                models.User.is_active.is_(True),
                models.User.deleted_at.is_(None))
        .all()
    )
    ids = [u.id for u in rows if u.id != exclude_user_id]
    return notify_many(db, user_ids=ids, firm_id=firm_id, event_type=event_type,
                       title=title, message=message, link=link,
                       entity_type=entity_type, entity_id=entity_id)


def unread_count(db: Session, user_id: str) -> int:
    return (
        db.query(models.Notification)
        .filter(models.Notification.user_id == user_id,
                models.Notification.read_status.is_(False))
        .count()
    )


def notify_client_owner(
    db: Session, *, client: models.Client, event_type: str, title: str,
    message: Optional[str] = None, link: Optional[str] = None,
    entity_type: Optional[str] = None, entity_id: Optional[str] = None,
    once: bool = False, force: bool = False,
) -> int:
    """Tell whoever is responsible for a client. Returns how many were notified.

    That is the assigned associate. If nobody is assigned, or the assignee has
    been deactivated, it falls back to everyone who can approve (Manager and
    above), because an alert that reaches no one is the failure this system
    exists to prevent: a fetch that failed silently is worse than one that
    failed loudly to the wrong person.
    """
    assignee = (db.get(models.User, client.assigned_associate_id)
                if client.assigned_associate_id else None)
    if assignee and assignee.is_active and assignee.deleted_at is None:
        row = notify(
            db, user_id=assignee.id, firm_id=client.firm_id, event_type=event_type,
            title=title, message=message, link=link, entity_type=entity_type,
            entity_id=entity_id, once=once, force=force,
        )
        return 1 if row is not None else 0

    approver_roles = (models.UserRole.MANAGER, models.UserRole.PARTNER, models.UserRole.OWNER)
    approvers = (
        db.query(models.User)
        .filter(models.User.firm_id == client.firm_id,
                models.User.role.in_(approver_roles),
                models.User.is_active.is_(True),
                models.User.deleted_at.is_(None))
        .all()
    )
    created = 0
    for user in approvers:
        row = notify(
            db, user_id=user.id, firm_id=client.firm_id, event_type=event_type,
            title=title, message=message, link=link, entity_type=entity_type,
            entity_id=entity_id, once=once, force=force,
        )
        created += 1 if row is not None else 0
    return created
