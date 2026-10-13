"""
Notification delivery, preferences and tenant scoping.
"""
import pytest

from app import models
from app.notifications import (
    EVENT_CATALOGUE, NotificationType, notify, notify_approvers, resolve_preference,
    unread_count,
)
from tests.conftest import TEST_PASSWORD


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------

def test_notify_creates_an_unread_notification(db, two_firms):
    a = two_firms["a"]
    row = notify(
        db, user_id=a["users"]["associate"].id, firm_id=a["firm"].id,
        event_type=NotificationType.EXCEPTION_ASSIGNED,
        title="Work for you", message="An exception needs attention.",
        entity_type="ReconciliationException", entity_id=a["exception"].id,
    )
    db.commit()
    assert row is not None
    assert row.read_status is False
    assert unread_count(db, a["users"]["associate"].id) == 1


def test_duplicate_about_the_same_entity_is_suppressed(db, two_firms):
    """A retried job must not notify the same person twice about one thing."""
    a = two_firms["a"]
    kwargs = dict(
        user_id=a["users"]["associate"].id, firm_id=a["firm"].id,
        event_type=NotificationType.GSTR2B_FETCH_COMPLETED,
        title="GSTR-2B received",
        entity_type="GSTR2BFetch", entity_id="fetch-123",
    )
    first = notify(db, **kwargs)
    second = notify(db, **kwargs)
    db.commit()

    assert first is not None
    assert second is None, "the repeat should have been suppressed"
    assert unread_count(db, a["users"]["associate"].id) == 1


def test_force_overrides_deduplication(db, two_firms):
    """Each new comment is genuinely new information."""
    a = two_firms["a"]
    kwargs = dict(
        user_id=a["users"]["associate"].id, firm_id=a["firm"].id,
        event_type=NotificationType.EXCEPTION_COMMENT,
        title="New comment",
        entity_type="ReconciliationException", entity_id=a["exception"].id,
    )
    assert notify(db, **kwargs) is not None
    assert notify(db, force=True, **kwargs) is not None
    db.commit()
    assert unread_count(db, a["users"]["associate"].id) == 2


def test_notification_carries_the_firm_of_the_recipient(db, two_firms):
    a = two_firms["a"]
    row = notify(
        db, user_id=a["users"]["owner"].id, firm_id=a["firm"].id,
        event_type=NotificationType.RECONCILIATION_COMPLETED, title="Done",
    )
    db.commit()
    assert row.firm_id == a["firm"].id


def test_notify_approvers_reaches_managers_and_above_only(db, two_firms):
    a = two_firms["a"]
    created = notify_approvers(
        db, firm_id=a["firm"].id,
        event_type=NotificationType.EXCEPTION_NEEDS_APPROVAL,
        title="Needs approval",
        entity_type="ReconciliationException", entity_id=a["exception"].id,
    )
    db.commit()
    # The fixture firm has an owner, a manager and an associate.
    assert created == 2
    assert unread_count(db, a["users"]["associate"].id) == 0
    assert unread_count(db, a["users"]["manager"].id) == 1
    assert unread_count(db, a["users"]["owner"].id) == 1


def test_notify_approvers_excludes_the_person_who_raised_it(db, two_firms):
    a = two_firms["a"]
    created = notify_approvers(
        db, firm_id=a["firm"].id,
        event_type=NotificationType.EXCEPTION_NEEDS_APPROVAL,
        title="Needs approval",
        entity_type="ReconciliationException", entity_id=a["exception"].id,
        exclude_user_id=a["users"]["manager"].id,
    )
    db.commit()
    assert created == 1
    assert unread_count(db, a["users"]["manager"].id) == 0


def test_approvers_of_one_firm_are_never_notified_about_another(db, two_firms):
    a, b = two_firms["a"], two_firms["b"]
    notify_approvers(
        db, firm_id=a["firm"].id,
        event_type=NotificationType.EXCEPTION_NEEDS_APPROVAL, title="Firm A only",
    )
    db.commit()
    for user in b["users"].values():
        assert unread_count(db, user.id) == 0


def test_preferences_fall_back_to_the_catalogue_default(db, two_firms):
    a = two_firms["a"]
    in_app, email = resolve_preference(
        db, a["users"]["owner"].id, NotificationType.EXCEPTION_APPROVED
    )
    spec = EVENT_CATALOGUE[NotificationType.EXCEPTION_APPROVED]
    assert in_app is spec.default_in_app
    assert email is spec.default_email


def test_turning_an_event_off_entirely_suppresses_it(db, two_firms):
    a = two_firms["a"]
    user = a["users"]["associate"]
    db.add(models.NotificationPreference(
        firm_id=a["firm"].id, user_id=user.id,
        event_type=NotificationType.EXCEPTION_ASSIGNED,
        in_app=False, email=False,
    ))
    db.commit()

    row = notify(
        db, user_id=user.id, firm_id=a["firm"].id,
        event_type=NotificationType.EXCEPTION_ASSIGNED, title="Ignored",
    )
    db.commit()
    assert row is None
    assert unread_count(db, user.id) == 0


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def test_notification_endpoints_require_auth(client):
    assert client.get("/api/notifications").status_code == 401
    assert client.get("/api/notifications/unread-count").status_code == 401


def test_user_sees_only_their_own_notifications(client, login, db, two_firms):
    a = two_firms["a"]
    notify(db, user_id=a["users"]["owner"].id, firm_id=a["firm"].id,
           event_type=NotificationType.RECONCILIATION_COMPLETED,
           title="For the owner")
    notify(db, user_id=a["users"]["associate"].id, firm_id=a["firm"].id,
           event_type=NotificationType.RECONCILIATION_COMPLETED,
           title="For the associate")
    db.commit()

    headers = login("owner@alpha-ca.in")
    rows = client.get("/api/notifications", headers=headers).json()
    titles = {r["title"] for r in rows}
    assert "For the owner" in titles
    assert "For the associate" not in titles, (
        "a colleague's notifications must not be visible, even within the same firm"
    )


def test_cannot_mark_a_colleagues_notification_read(client, login, db, two_firms):
    a = two_firms["a"]
    row = notify(db, user_id=a["users"]["associate"].id, firm_id=a["firm"].id,
                 event_type=NotificationType.RECONCILIATION_COMPLETED,
                 title="Associate only")
    db.commit()

    headers = login("owner@alpha-ca.in")
    resp = client.post(f"/api/notifications/{row.id}/read", headers=headers)
    assert resp.status_code == 404


def test_cannot_read_another_firms_notification(client, login, db, two_firms):
    b = two_firms["b"]
    row = notify(db, user_id=b["users"]["owner"].id, firm_id=b["firm"].id,
                 event_type=NotificationType.RECONCILIATION_COMPLETED,
                 title="Firm B secret")
    db.commit()

    headers = login("owner@alpha-ca.in")
    assert client.post(f"/api/notifications/{row.id}/read",
                       headers=headers).status_code == 404


def test_unread_count_and_mark_all_read(client, login, db, two_firms):
    a = two_firms["a"]
    for i in range(3):
        notify(db, user_id=a["users"]["owner"].id, firm_id=a["firm"].id,
               event_type=NotificationType.RECONCILIATION_COMPLETED,
               title=f"Item {i}", entity_type="Run", entity_id=f"run-{i}")
    db.commit()

    headers = login("owner@alpha-ca.in")
    assert client.get("/api/notifications/unread-count",
                      headers=headers).json()["unread"] == 3

    resp = client.post("/api/notifications/read-all", headers=headers)
    assert resp.status_code == 200
    assert client.get("/api/notifications/unread-count",
                      headers=headers).json()["unread"] == 0


def test_preferences_list_covers_the_whole_catalogue(client, login, two_firms):
    headers = login("owner@alpha-ca.in")
    rows = client.get("/api/notifications/preferences", headers=headers).json()
    assert len(rows) == len(EVENT_CATALOGUE)
    assert all(r["label"] and r["description"] for r in rows)


def test_updating_a_preference_persists_and_takes_effect(client, login, db, two_firms):
    a = two_firms["a"]
    headers = login("owner@alpha-ca.in")

    resp = client.put("/api/notifications/preferences", headers=headers, json={
        "preferences": [{
            "event_type": NotificationType.RECONCILIATION_COMPLETED,
            "in_app": False, "email": False,
        }]
    })
    assert resp.status_code == 200
    changed = next(r for r in resp.json()
                   if r["event_type"] == NotificationType.RECONCILIATION_COMPLETED)
    assert changed["in_app"] is False and changed["email"] is False

    row = notify(db, user_id=a["users"]["owner"].id, firm_id=a["firm"].id,
                 event_type=NotificationType.RECONCILIATION_COMPLETED,
                 title="Should be suppressed")
    db.commit()
    assert row is None


def test_unknown_event_type_is_rejected(client, login, two_firms):
    headers = login("owner@alpha-ca.in")
    resp = client.put("/api/notifications/preferences", headers=headers, json={
        "preferences": [{"event_type": "NOT_A_REAL_EVENT", "in_app": True, "email": True}]
    })
    assert resp.status_code == 422
    assert "NOT_A_REAL_EVENT" in resp.text


# ---------------------------------------------------------------------------
# Wired into the real workflow
# ---------------------------------------------------------------------------

def test_resolving_an_exception_notifies_approvers(client, login, db, two_firms):
    a = two_firms["a"]
    associate = login("associate@alpha-ca.in")

    resp = client.patch(f"/api/exceptions/{a['exception'].id}/resolve",
                        json={"resolution_notes": "Timing difference."},
                        headers=associate)
    assert resp.status_code == 200

    # The manager and owner should now have something waiting.
    assert unread_count(db, a["users"]["manager"].id) >= 1
    assert unread_count(db, a["users"]["owner"].id) >= 1
    # The associate who raised it should not be told about their own work.
    assert unread_count(db, a["users"]["associate"].id) == 0


def test_approval_notifies_the_person_who_resolved_it(client, login, db, two_firms):
    a = two_firms["a"]
    associate = login("associate@alpha-ca.in")
    manager = login("manager@alpha-ca.in")

    client.patch(f"/api/exceptions/{a['exception'].id}/resolve",
                 json={"resolution_notes": "Timing difference."}, headers=associate)
    resp = client.patch(f"/api/exceptions/{a['exception'].id}/approve",
                        json={"approved": True}, headers=manager)
    assert resp.status_code == 200

    rows = client.get("/api/notifications", headers=associate).json()
    assert any(r["type"] == NotificationType.EXCEPTION_APPROVED for r in rows)


def test_rework_notifies_with_the_reason(client, login, two_firms):
    a = two_firms["a"]
    associate = login("associate@alpha-ca.in")
    manager = login("manager@alpha-ca.in")

    client.patch(f"/api/exceptions/{a['exception'].id}/resolve",
                 json={"resolution_notes": "Timing difference."}, headers=associate)
    client.patch(f"/api/exceptions/{a['exception'].id}/approve",
                 json={"approved": False, "rework_reason": "Attach the supplier email."},
                 headers=manager)

    rows = client.get("/api/notifications", headers=associate).json()
    rework = [r for r in rows if r["type"] == NotificationType.EXCEPTION_REWORK]
    assert rework
    assert "supplier email" in rework[0]["message"]


def test_assignment_notifies_the_assignee(client, login, two_firms):
    a = two_firms["a"]
    manager = login("manager@alpha-ca.in")
    associate_id = a["users"]["associate"].id

    resp = client.patch(f"/api/exceptions/{a['exception'].id}/assign",
                        json={"assigned_to": associate_id}, headers=manager)
    assert resp.status_code == 200

    associate = login("associate@alpha-ca.in")
    rows = client.get("/api/notifications", headers=associate).json()
    assert any(r["type"] == NotificationType.EXCEPTION_ASSIGNED for r in rows)


# ---------------------------------------------------------------------------
# Resend email adapter (HTTP mocked; nothing here touches the network)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text or str(self._body)

    def json(self):
        return self._body


@pytest.fixture
def resend_configured(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "resend_api_key", "re_test_key")
    monkeypatch.setattr(settings, "email_from", "alerts@firm.example")


def test_resend_posts_the_expected_payload(monkeypatch, resend_configured):
    import httpx
    from app.services.messaging import EmailMessage, ResendEmailProvider

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _FakeResponse(200, {"id": "email_abc123"})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = ResendEmailProvider().send(
        EmailMessage(to="assoc@firm.example", subject="Hello", body="Body text")
    )

    assert result.ok is True
    assert result.reference == "email_abc123"
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test_key"
    assert captured["json"]["to"] == ["assoc@firm.example"]
    assert captured["json"]["from"] == "alerts@firm.example"
    assert captured["json"]["subject"] == "Hello"
    assert captured["timeout"], "a request without a timeout can hang a worker forever"


def test_resend_api_rejection_is_reported_not_raised(monkeypatch, resend_configured):
    import httpx
    from app.services.messaging import EmailMessage, ResendEmailProvider

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(
        403, text='{"message":"The firm.example domain is not verified"}'))
    result = ResendEmailProvider().send(
        EmailMessage(to="x@firm.example", subject="s", body="b"))

    assert result.ok is False
    assert "403" in result.error
    assert "not verified" in result.error, "the provider's reason should reach the log"


def test_resend_network_failure_is_reported_not_raised(monkeypatch, resend_configured):
    import httpx
    from app.services.messaging import EmailMessage, ResendEmailProvider

    def boom(*a, **k):
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(httpx, "post", boom)
    result = ResendEmailProvider().send(
        EmailMessage(to="x@firm.example", subject="s", body="b"))
    assert result.ok is False
    assert "timed out" in result.error


def test_resend_without_a_key_fails_cleanly(monkeypatch):
    from app.config import settings
    from app.services.messaging import EmailMessage, ResendEmailProvider

    monkeypatch.setattr(settings, "resend_api_key", "")
    result = ResendEmailProvider().send(
        EmailMessage(to="x@firm.example", subject="s", body="b"))
    assert result.ok is False
    assert "RESEND_API_KEY" in result.error


def test_an_email_outage_never_loses_the_in_app_notification(monkeypatch, db, two_firms):
    """The whole point of in-app being the record and email a copy."""
    from app.config import settings
    from app.services import messaging

    class ExplodingProvider(messaging.EmailProvider):
        name = "exploding"

        def send(self, message):
            raise RuntimeError("mail server on fire")

    monkeypatch.setattr(messaging, "get_email_provider", lambda: ExplodingProvider())

    a = two_firms["a"]
    row = notify(
        db, user_id=a["users"]["manager"].id, firm_id=a["firm"].id,
        event_type=NotificationType.EXCEPTION_NEEDS_APPROVAL,   # emails by default
        title="Approval needed", entity_type="ReconciliationException", entity_id="e-1",
    )
    db.commit()

    assert row is not None, "the request must not fail because mail did"
    assert row.emailed_at is None, "a failed send must not be recorded as sent"
    assert unread_count(db, a["users"]["manager"].id) == 1


def test_successful_send_records_emailed_at(monkeypatch, db, two_firms):
    from app.services import messaging

    monkeypatch.setattr(messaging, "get_email_provider",
                        lambda: messaging.NullEmailProvider())
    a = two_firms["a"]
    row = notify(
        db, user_id=a["users"]["manager"].id, firm_id=a["firm"].id,
        event_type=NotificationType.EXCEPTION_NEEDS_APPROVAL,
        title="Approval needed", entity_type="ReconciliationException", entity_id="e-2",
    )
    db.commit()
    assert row.emailed_at is not None
