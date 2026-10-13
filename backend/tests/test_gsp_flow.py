"""
The consent API: connecting a GSTIN with an OTP, fetching on demand, and
disconnecting. Every route is also probed from a second firm.
"""
from datetime import datetime, timedelta

import pytest

from app import models
from app.config import settings
from app.crypto import decrypt
from app.gsp.base import (
    GSPResponseError, GSPUnavailableError, OTPRequestError,
)
from tests.gsp_helpers import GSTIN, VENDOR, FakeGSP, make_document, make_invoice

PERIOD = "2026-03"


@pytest.fixture
def fake_gsp(client):
    from app.routers.gsp import get_provider
    fake = FakeGSP()
    client.app.dependency_overrides[get_provider] = lambda: fake
    return fake


def gsp(gid, suffix=""):
    return f"/api/gstins/{gid}/gsp{suffix}"


def request_otp(client, headers, gid, username="portal.user"):
    return client.post(gsp(gid, "/consent/request"), json={"username": username}, headers=headers)


def verify(client, headers, gid, attempt_id, otp):
    return client.post(gsp(gid, "/consent/verify"),
                       json={"attempt_id": attempt_id, "otp": otp}, headers=headers)


def connect(client, headers, gid, fake):
    attempt = request_otp(client, headers, gid).json()["attempt_id"]
    resp = verify(client, headers, gid, attempt, fake.otp)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Connecting
# ---------------------------------------------------------------------------

def test_full_connect_flow(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    gid = a["gstin"].id

    assert client.get(gsp(gid), headers=headers).json()["state"] == "NONE"

    req = request_otp(client, headers, gid)
    assert req.status_code == 200, req.text
    assert req.json()["provider"] == "fake"
    assert fake_gsp.called("initiate")[0][1:] == (a["gstin"].gstin, "portal.user")

    done = verify(client, headers, gid, req.json()["attempt_id"], fake_gsp.otp)
    assert done.status_code == 200, done.text
    status = done.json()["status"]
    assert status["state"] == "ACTIVE"
    assert status["portal_username"] == "portal.user"
    assert status["connected_provider"] == "fake"


def test_the_two_expiries_come_from_the_provider_and_differ(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    fake_gsp.session_hours, fake_gsp.token_hours = 24 * 30, 6
    status = connect(client, login("associate@alpha-ca.in"), a["gstin"].id, fake_gsp)["status"]

    session_end = datetime.fromisoformat(status["session_expires_at"])
    token_end = datetime.fromisoformat(status["token_expires_at"])
    assert session_end - token_end > timedelta(days=29), "the consent window is not the token life"
    assert timedelta(days=29, hours=23) < session_end - datetime.utcnow() < timedelta(days=30, hours=1)


def test_the_consent_window_is_never_assumed(client, login, two_firms, fake_gsp):
    """Two providers, two windows: each is stored exactly as reported."""
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    fake_gsp.session_hours = 6
    short = connect(client, headers, a["gstin"].id, fake_gsp)["status"]
    remaining_short = datetime.fromisoformat(short["session_expires_at"]) - datetime.utcnow()

    fake_gsp.session_hours = 24 * 15
    long_ = connect(client, headers, a["gstin"].id, fake_gsp)["status"]
    remaining_long = datetime.fromisoformat(long_["session_expires_at"]) - datetime.utcnow()

    assert remaining_short < timedelta(hours=6, minutes=1)
    assert timedelta(days=14, hours=23) < remaining_long < timedelta(days=15, hours=1)


def test_the_token_is_stored_encrypted_and_never_returned(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    gid = a["gstin"].id

    req = request_otp(client, headers, gid)
    done = verify(client, headers, gid, req.json()["attempt_id"], fake_gsp.otp)
    everything = "".join([
        req.text, done.text, client.get(gsp(gid), headers=headers).text,
        client.get(gsp(gid, "/attempts"), headers=headers).text,
        client.get(f"/api/clients/{a['client'].id}", headers=headers).text,
        client.get(f"/api/clients/{a['client'].id}/gstins", headers=headers).text,
    ])
    assert "fake-token" not in everything, "the access token appeared in an API response"

    db.expire_all()
    stored = db.get(models.ClientGSTIN, gid).gsp_consent_token
    assert stored and "fake-token" not in stored, "stored in plaintext"
    assert decrypt(stored) == "fake-token-1"


def test_a_grant_is_recorded_for_data_protection_and_audit(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    connect(client, login("associate@alpha-ca.in"), a["gstin"].id, fake_gsp)

    db.expire_all()
    log = db.query(models.ConsentLog).one()
    assert log.consent_type == models.ConsentType.GSP_GSTR_ACCESS
    assert log.granted_via == "GSP_OTP" and log.withdrawn_at is None
    assert log.client_gstin_id == a["gstin"].id and log.expires_at is not None
    actions = {e.action for e in db.query(models.AuditLog).filter_by(firm_id=a["firm"].id)}
    assert {"GSP_CONSENT_REQUESTED", "CONSENT_GRANTED"} <= actions


def test_the_otp_is_never_stored_or_audited(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    connect(client, login("associate@alpha-ca.in"), a["gstin"].id, fake_gsp)
    db.expire_all()
    haystack = " ".join(
        str(e.meta) for e in db.query(models.AuditLog).filter_by(firm_id=a["firm"].id))
    haystack += " ".join(
        f"{x.username}{x.failure_code}{x.provider_transaction_id}"
        for x in db.query(models.GSPConsentAttempt))
    assert fake_gsp.otp not in haystack


# ---------------------------------------------------------------------------
# OTP handling
# ---------------------------------------------------------------------------

def test_wrong_otps_are_limited_then_a_new_otp_is_required(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    gid = a["gstin"].id
    attempt = request_otp(client, headers, gid).json()["attempt_id"]

    first = verify(client, headers, gid, attempt, "000000")
    assert first.status_code == 422 and "2 attempts left" in first.json()["detail"]
    second = verify(client, headers, gid, attempt, "000000")
    assert second.status_code == 422 and "1 attempt left" in second.json()["detail"]
    third = verify(client, headers, gid, attempt, "000000")
    assert third.status_code == 429 and "Request a new OTP" in third.json()["detail"]

    # Even the right OTP is refused once the request is closed.
    closed = verify(client, headers, gid, attempt, fake_gsp.otp)
    assert closed.status_code == 409
    db.expire_all()
    assert db.get(models.GSPConsentAttempt, attempt).status == models.ConsentAttemptStatus.FAILED
    assert client.get(gsp(gid), headers=headers).json()["state"] == "NONE"


def test_a_correct_otp_after_a_mistake_still_works(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    gid = a["gstin"].id
    attempt = request_otp(client, headers, gid).json()["attempt_id"]
    assert verify(client, headers, gid, attempt, "000000").status_code == 422
    assert verify(client, headers, gid, attempt, fake_gsp.otp).status_code == 200


def test_a_malformed_otp_is_rejected_without_calling_the_provider(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    attempt = request_otp(client, headers, a["gstin"].id).json()["attempt_id"]
    for bad in ("abc123", "12", "123456789", "12 34"):
        resp = verify(client, headers, a["gstin"].id, attempt, bad)
        assert resp.status_code in (422,), (bad, resp.text)
    assert fake_gsp.called("verify") == []


def test_an_otp_entered_after_its_window_is_refused(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    attempt = request_otp(client, headers, a["gstin"].id).json()["attempt_id"]
    row = db.get(models.GSPConsentAttempt, attempt)
    row.expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()

    resp = verify(client, headers, a["gstin"].id, attempt, fake_gsp.otp)
    assert resp.status_code == 409 and "expired" in resp.json()["detail"]
    assert fake_gsp.called("verify") == []


def test_a_new_request_supersedes_the_previous_one(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    first = request_otp(client, headers, a["gstin"].id).json()["attempt_id"]
    second = request_otp(client, headers, a["gstin"].id).json()["attempt_id"]

    assert verify(client, headers, a["gstin"].id, first, fake_gsp.otp).status_code == 409
    assert verify(client, headers, a["gstin"].id, second, fake_gsp.otp).status_code == 200


def test_otp_requests_are_throttled_to_protect_the_clients_phone(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    limit = settings.gsp_otp_max_requests_per_hour
    for _ in range(limit):
        assert request_otp(client, headers, a["gstin"].id).status_code == 200

    blocked = request_otp(client, headers, a["gstin"].id)
    assert blocked.status_code == 429 and "text" in blocked.json()["detail"].lower()
    assert len(fake_gsp.called("initiate")) == limit, "the blocked request must not reach the portal"


def test_a_provider_outage_on_verify_does_not_cost_an_attempt(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    gid = a["gstin"].id
    attempt = request_otp(client, headers, gid).json()["attempt_id"]

    fake_gsp.verify_error = [GSPUnavailableError("The GST service is down.")]
    down = verify(client, headers, gid, attempt, fake_gsp.otp)
    assert down.status_code == 503

    db.expire_all()
    row = db.get(models.GSPConsentAttempt, attempt)
    assert row.verify_attempts == 0 and row.status == models.ConsentAttemptStatus.PENDING
    assert verify(client, headers, gid, attempt, fake_gsp.otp).status_code == 200


def test_the_portal_refusing_to_issue_an_otp_is_explained(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    fake_gsp.initiate_error = OTPRequestError("That GST portal username was not recognised.")
    resp = request_otp(client, login("associate@alpha-ca.in"), a["gstin"].id)
    assert resp.status_code == 422 and "not recognised" in resp.json()["detail"]
    assert db.query(models.GSPConsentAttempt).count() == 0


@pytest.mark.parametrize("username", ["", "a", "bad name", "x;drop", "a" * 101])
def test_a_malformed_portal_username_is_rejected(client, login, two_firms, fake_gsp, username):
    a = two_firms["a"]
    resp = request_otp(client, login("associate@alpha-ca.in"), a["gstin"].id, username)
    assert resp.status_code == 422
    assert fake_gsp.called("initiate") == []


def test_a_short_window_explains_what_to_change_on_the_portal(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    fake_gsp.session_hours = 6
    done = connect(client, login("associate@alpha-ca.in"), a["gstin"].id, fake_gsp)
    assert "6 hours" in done["window_note"]
    assert "Manage API Access" in done["window_note"]


def test_a_long_window_carries_no_warning(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    fake_gsp.session_hours = 24 * 30
    assert connect(client, login("associate@alpha-ca.in"), a["gstin"].id, fake_gsp)["window_note"] is None


# ---------------------------------------------------------------------------
# Disconnecting
# ---------------------------------------------------------------------------

def test_only_a_manager_can_disconnect(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    connect(client, login("associate@alpha-ca.in"), a["gstin"].id, fake_gsp)

    assert client.delete(gsp(a["gstin"].id, "/consent"),
                         headers=login("associate@alpha-ca.in")).status_code == 403

    resp = client.delete(gsp(a["gstin"].id, "/consent"), headers=login("manager@alpha-ca.in"))
    assert resp.status_code == 200 and resp.json()["state"] == "NONE"

    db.expire_all()
    g = db.get(models.ClientGSTIN, a["gstin"].id)
    assert g.gsp_consent_token is None and g.consent_expires_at is None
    assert db.query(models.ConsentLog).one().withdrawn_at is not None
    assert "CONSENT_WITHDRAWN" in {e.action for e in db.query(models.AuditLog)}


def test_the_provider_is_not_contacted_after_disconnecting(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    connect(client, login("associate@alpha-ca.in"), a["gstin"].id, fake_gsp)
    client.delete(gsp(a["gstin"].id, "/consent"), headers=login("manager@alpha-ca.in"))
    fake_gsp.calls.clear()

    out = client.post(gsp(a["gstin"].id, f"/fetch?period={PERIOD}"),
                      headers=login("associate@alpha-ca.in")).json()
    assert out["status"] == "FAILED" and out["error_code"] == "NOT_CONNECTED"
    assert fake_gsp.calls == []


# ---------------------------------------------------------------------------
# Fetching now
# ---------------------------------------------------------------------------

def test_fetch_now_stores_reconciles_and_reports(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    connect(client, headers, a["gstin"].id, fake_gsp)
    # Matches the purchase register entry the fixture created for this GSTIN.
    fake_gsp.document = make_document(GSTIN, PERIOD, [
        make_invoice("INV-alpha-001", vendor=VENDOR, taxable=10000.0, day=10)])

    out = client.post(gsp(a["gstin"].id, f"/fetch?period={PERIOD}"), headers=headers).json()
    assert out["status"] == "SUCCESS" and out["reconciled"] is True
    assert out["invoice_count"] == 1

    summary = client.get(f"/api/gstins/{a['gstin'].id}/reconciliation/{PERIOD}/summary",
                         headers=headers).json()
    assert summary["counts_by_status"] == {"MATCHED": 1}

    fetches = client.get(f"/api/gstins/{a['gstin'].id}/gstr2b/fetches", headers=headers).json()
    assert fetches[0]["source"] == "API" and fetches[0]["gsp_provider"] == "fake"


def test_a_manual_fetch_records_who_asked_and_notifies_nobody(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    connect(client, headers, a["gstin"].id, fake_gsp)
    client.post(gsp(a["gstin"].id, f"/fetch?period={PERIOD}"), headers=headers)

    db.expire_all()
    fetch = db.query(models.GSTR2BFetch).one()
    assert fetch.fetched_by == a["users"]["associate"].id
    assert db.query(models.Notification).filter(
        models.Notification.type == "GSTR2B_FETCH_COMPLETED").count() == 0


def test_fetch_failures_are_reported_to_the_person_waiting(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    connect(client, headers, a["gstin"].id, fake_gsp)
    fake_gsp.fetch_error = GSPResponseError("The provider sent something odd.",
                                            transaction_id="txn-xyz")
    out = client.post(gsp(a["gstin"].id, f"/fetch?period={PERIOD}"), headers=headers).json()
    assert out["status"] == "FAILED" and "txn-xyz" in out["message"]


@pytest.mark.parametrize("period", ["2026-13", "2026-3", "March", "", "2026-00"])
def test_fetch_rejects_a_malformed_period(client, login, two_firms, fake_gsp, period):
    a = two_firms["a"]
    resp = client.post(gsp(a["gstin"].id, f"/fetch?period={period}"),
                       headers=login("associate@alpha-ca.in"))
    assert resp.status_code == 422
    assert fake_gsp.calls == []


def test_fetch_rejects_a_future_period(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    future = (datetime.utcnow() + timedelta(days=90)).strftime("%Y-%m")
    resp = client.post(gsp(a["gstin"].id, f"/fetch?period={future}"),
                       headers=login("associate@alpha-ca.in"))
    assert resp.status_code == 422 and "future" in resp.json()["detail"]


def test_fetch_history_lists_attempts(client, login, two_firms, fake_gsp):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    connect(client, headers, a["gstin"].id, fake_gsp)
    client.post(gsp(a["gstin"].id, f"/fetch?period={PERIOD}"), headers=headers)

    rows = client.get(gsp(a["gstin"].id, "/attempts"), headers=headers).json()
    assert len(rows) == 1 and rows[0]["status"] == "SUCCESS" and rows[0]["trigger"] == "MANUAL"
    status = client.get(gsp(a["gstin"].id), headers=headers).json()
    assert status["recent_attempts"][0]["period"] == PERIOD


# ---------------------------------------------------------------------------
# GSTIN verification
# ---------------------------------------------------------------------------

def test_verify_gstin_records_what_the_register_says(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    out = client.post(gsp(a["gstin"].id, "/verify-gstin"),
                      headers=login("associate@alpha-ca.in")).json()
    assert out["legal_name"] == "Fake Legal Pvt Ltd" and out["is_active"] is True

    db.expire_all()
    g = db.get(models.ClientGSTIN, a["gstin"].id)
    assert g.legal_name == "Fake Legal Pvt Ltd" and g.verified_at is not None


def test_a_cancelled_registration_carries_a_warning(client, login, two_firms, fake_gsp):
    from app.gsp.base import GSTINInfo
    a = two_firms["a"]
    fake_gsp.verify_gstin = lambda g: GSTINInfo(
        gstin=g, legal_name="Gone Ltd", trade_name=None, status="Cancelled")
    out = client.post(gsp(a["gstin"].id, "/verify-gstin"),
                      headers=login("associate@alpha-ca.in")).json()
    assert out["is_active"] is False and "Cancelled" in out["warning"]


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def test_unauthenticated_access_is_refused(client, two_firms, fake_gsp):
    gid = two_firms["a"]["gstin"].id
    for method, path in [("get", gsp(gid)), ("post", gsp(gid, "/consent/request")),
                         ("post", gsp(gid, f"/fetch?period={PERIOD}")),
                         ("delete", gsp(gid, "/consent"))]:
        assert getattr(client, method)(path).status_code == 401, path
    assert fake_gsp.calls == []


# ---------------------------------------------------------------------------
# Tenant isolation: every route, from the other firm
# ---------------------------------------------------------------------------

def test_another_firms_gstin_is_invisible_on_every_gsp_route(client, login, two_firms, fake_gsp, db):
    a, b = two_firms["a"], two_firms["b"]
    foreign = b["gstin"].id
    attempt = models.GSPConsentAttempt(
        firm_id=b["firm"].id, client_gstin_id=foreign, provider="fake", username="b.user",
        requested_by=b["users"]["owner"].id, expires_at=datetime.utcnow() + timedelta(minutes=5))
    db.add(attempt)
    db.commit()

    manager = login("manager@alpha-ca.in")
    probes = [
        ("get", gsp(foreign), None),
        ("get", gsp(foreign, "/attempts"), None),
        ("post", gsp(foreign, "/verify-gstin"), None),
        ("post", gsp(foreign, "/consent/request"), {"username": "someone"}),
        ("post", gsp(foreign, "/consent/verify"), {"attempt_id": attempt.id, "otp": fake_gsp.otp}),
        ("delete", gsp(foreign, "/consent"), None),
        ("post", gsp(foreign, f"/fetch?period={PERIOD}"), None),
    ]
    for method, path, body in probes:
        kwargs = {"headers": manager, **({"json": body} if body is not None else {})}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code == 404, f"{method.upper()} {path} answered {resp.status_code}"
    assert fake_gsp.calls == [], "the provider must never be contacted on another firm's behalf"


def test_an_otp_attempt_cannot_be_used_against_a_different_gstin(client, login, two_firms, fake_gsp, db):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    second = models.ClientGSTIN(firm_id=a["firm"].id, client_id=a["client"].id,
                                gstin="07AAACH1925Q1ZS", state_code="07")
    db.add(second)
    db.commit()

    attempt = request_otp(client, headers, a["gstin"].id).json()["attempt_id"]
    resp = verify(client, headers, second.id, attempt, fake_gsp.otp)
    assert resp.status_code == 404
    assert fake_gsp.called("verify") == []


# ---------------------------------------------------------------------------
# The bundled mock provider, end to end (no override)
# ---------------------------------------------------------------------------

def test_the_mock_provider_supports_the_whole_flow_locally(client, login, two_firms, monkeypatch):
    monkeypatch.setattr(settings, "gsp_provider", "mock")
    monkeypatch.setattr(settings, "environment", "development")
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    gid = a["gstin"].id

    status = client.get(gsp(gid), headers=headers).json()
    assert status["provider_is_mock"] is True and status["state"] == "NONE"

    attempt = request_otp(client, headers, gid).json()["attempt_id"]
    assert verify(client, headers, gid, attempt, "654321").status_code == 422
    assert verify(client, headers, gid, attempt, "123456").json()["status"]["state"] == "ACTIVE"

    out = client.post(gsp(gid, f"/fetch?period={PERIOD}"), headers=headers).json()
    assert out["status"] == "SUCCESS" and out["invoice_count"] == 34
    assert out["credit_note_count"] == 1


def test_the_mock_provider_is_refused_outside_development(client, login, two_firms, monkeypatch):
    monkeypatch.setattr(settings, "gsp_provider", "mock")
    monkeypatch.setattr(settings, "environment", "production")
    resp = client.get(gsp(two_firms["a"]["gstin"].id), headers=login("associate@alpha-ca.in"))
    assert resp.status_code == 503 and "fabricated" in resp.json()["detail"]
