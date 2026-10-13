"""
Authentication, session management and role enforcement.
"""
import pytest

from app import models
from tests.conftest import TEST_PASSWORD


SIGNUP = {
    "firm_name": "New Firm & Co",
    "full_name": "Test Owner",
    "email": "newowner@newfirm-ca.in",
    "password": "a-long-enough-password",
}


def test_signup_creates_firm_and_owner(client):
    resp = client.post("/api/auth/signup", json=SIGNUP)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user"]["role"] == "OWNER"
    assert body["firm"]["name"] == "New Firm & Co"
    assert body["access_token"]
    # The refresh token must be an httpOnly cookie, never in the body.
    assert "refresh_token" not in body
    assert "gstr2b_refresh" in resp.cookies


def test_signup_rejects_weak_password(client):
    resp = client.post("/api/auth/signup", json={**SIGNUP, "password": "short"})
    assert resp.status_code == 422


def test_signup_rejects_duplicate_email(client):
    assert client.post("/api/auth/signup", json=SIGNUP).status_code == 201
    resp = client.post("/api/auth/signup", json=SIGNUP)
    assert resp.status_code == 409


def test_signup_rejects_invalid_firm_gstin(client):
    resp = client.post("/api/auth/signup",
                       json={**SIGNUP, "firm_gstin": "27AAPFU0939F1ZX"})
    assert resp.status_code == 422
    assert "check digit" in resp.text.lower()


def test_login_and_me(client, two_firms):
    resp = client.post("/api/auth/login",
                       json={"email": "owner@alpha-ca.in", "password": TEST_PASSWORD})
    assert resp.status_code == 200
    token = resp.json()["access_token"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "owner@alpha-ca.in"


def test_login_with_wrong_password_is_generic(client, two_firms):
    resp = client.post("/api/auth/login",
                       json={"email": "owner@alpha-ca.in", "password": "wrong-password"})
    assert resp.status_code == 401
    detail = resp.json()["detail"]

    # An unknown account must produce the identical message, so the endpoint
    # cannot be used to discover which email addresses are registered.
    unknown = client.post("/api/auth/login",
                          json={"email": "nobody@nowhere-ca.in", "password": "wrong-password"})
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == detail


def test_account_locks_after_repeated_failures(client, two_firms, db):
    from app.config import settings
    for _ in range(settings.max_failed_logins):
        client.post("/api/auth/login",
                    json={"email": "associate@alpha-ca.in", "password": "wrong-password"})

    # Even the correct password is refused while the lock is in force.
    resp = client.post("/api/auth/login",
                       json={"email": "associate@alpha-ca.in", "password": TEST_PASSWORD})
    assert resp.status_code == 423
    assert "locked" in resp.json()["detail"].lower()


def test_invalid_token_is_rejected(client, two_firms):
    resp = client.get("/api/clients", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_refresh_rotates_the_token(client, two_firms):
    login = client.post("/api/auth/login",
                        json={"email": "owner@alpha-ca.in", "password": TEST_PASSWORD})
    first_cookie = login.cookies["gstr2b_refresh"]

    refreshed = client.post("/api/auth/refresh")
    assert refreshed.status_code == 200
    second_cookie = refreshed.cookies["gstr2b_refresh"]
    assert second_cookie != first_cookie, "refresh token must rotate on use"


def test_reusing_a_rotated_refresh_token_kills_the_session(client, two_firms):
    """Replaying a spent refresh token means it was captured, so every
    session for that user is revoked."""
    login = client.post("/api/auth/login",
                        json={"email": "owner@alpha-ca.in", "password": TEST_PASSWORD})
    stolen = login.cookies["gstr2b_refresh"]

    assert client.post("/api/auth/refresh").status_code == 200

    client.cookies.clear()
    client.cookies.set("gstr2b_refresh", stolen)
    replay = client.post("/api/auth/refresh")
    assert replay.status_code == 401
    assert "security" in replay.json()["detail"].lower()


def test_logout_revokes_the_refresh_token(client, two_firms):
    client.post("/api/auth/login",
                json={"email": "owner@alpha-ca.in", "password": TEST_PASSWORD})
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/auth/refresh").status_code == 401


def test_password_reset_request_never_reveals_account_existence(client, two_firms):
    known = client.post("/api/auth/password-reset/request",
                        json={"email": "owner@alpha-ca.in"})
    unknown = client.post("/api/auth/password-reset/request",
                          json={"email": "nobody@nowhere-ca.in"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def test_associate_cannot_invite_staff(client, login, two_firms):
    headers = login("associate@alpha-ca.in")
    resp = client.post("/api/auth/invitations",
                       json={"email": "newbie@alpha-ca.in", "role": "ASSOCIATE"},
                       headers=headers)
    assert resp.status_code == 403
    assert "Partner" in resp.json()["detail"]


def test_owner_can_invite_staff(client, login, two_firms):
    headers = login("owner@alpha-ca.in")
    resp = client.post("/api/auth/invitations",
                       json={"email": "newbie@alpha-ca.in", "role": "ASSOCIATE"},
                       headers=headers)
    assert resp.status_code == 201
    assert resp.json()["email"] == "newbie@alpha-ca.in"


def test_cannot_invite_a_second_owner(client, login, two_firms):
    headers = login("owner@alpha-ca.in")
    resp = client.post("/api/auth/invitations",
                       json={"email": "other@alpha-ca.in", "role": "OWNER"},
                       headers=headers)
    assert resp.status_code == 422


def test_client_viewer_invite_requires_a_client(client, login, two_firms):
    headers = login("owner@alpha-ca.in")
    resp = client.post("/api/auth/invitations",
                       json={"email": "viewer@alpha-ca.in", "role": "CLIENT_VIEWER"},
                       headers=headers)
    assert resp.status_code == 422


def test_associate_cannot_delete_a_client(client, login, two_firms):
    a = two_firms["a"]
    headers = login("associate@alpha-ca.in")
    resp = client.delete(f"/api/clients/{a['client'].id}", headers=headers)
    assert resp.status_code == 403


def test_manager_can_delete_a_client_softly(client, login, two_firms, db):
    a = two_firms["a"]
    headers = login("manager@alpha-ca.in")
    resp = client.delete(f"/api/clients/{a['client'].id}", headers=headers)
    assert resp.status_code == 200

    # Gone from the API, still present in the database.
    assert client.get(f"/api/clients/{a['client'].id}", headers=headers).status_code == 404
    db.expire_all()
    row = db.get(models.Client, a["client"].id)
    assert row is not None and row.deleted_at is not None


def test_associate_cannot_read_the_audit_log(client, login, two_firms):
    headers = login("associate@alpha-ca.in")
    assert client.get("/api/auth/audit-log", headers=headers).status_code == 403


def test_audit_log_records_logins_and_verifies(client, login, two_firms):
    headers = login("owner@alpha-ca.in")
    log = client.get("/api/auth/audit-log", headers=headers)
    assert log.status_code == 200
    actions = {e["action"] for e in log.json()}
    assert "LOGIN_SUCCESS" in actions

    verify = client.get("/api/auth/audit-log/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["valid"] is True


def test_audit_chain_detects_tampering(client, login, two_firms, db):
    """Editing a historical audit row must break the hash chain."""
    from app import audit

    headers = login("owner@alpha-ca.in")
    firm_id = two_firms["a"]["firm"].id
    assert audit.verify_chain(db, firm_id)["valid"] is True

    entry = (db.query(models.AuditLog)
             .filter(models.AuditLog.firm_id == firm_id)
             .order_by(models.AuditLog.created_at.asc()).first())
    entry.action = "SOMETHING_ELSE"
    db.commit()

    report = audit.verify_chain(db, firm_id)
    assert report["valid"] is False
    assert report["broken_at"] == entry.id
