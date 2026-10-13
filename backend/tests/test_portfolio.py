"""
The firm-wide reconciliation portfolio: which of a firm's clients need a human
this period, and which don't — the point of the "80 clients" pitch this
endpoint exists to answer.

Builds five clients in one firm, one for each status the portfolio
distinguishes, entirely through the real upload/reconcile/resolve HTTP
endpoints (not by poking rows into the database), so this test exercises the
same code paths a user does.
"""
import json

import pytest

from app import models
from app.security import hash_password
from tests.conftest import TEST_PASSWORD

PERIOD = "2026-05"


def _doc(gstin: str, invoices: list) -> bytes:
    return json.dumps({"gstin": gstin, "period": PERIOD, "generated_on": "2026-06-14",
                       "invoices": invoices}).encode()


def _csv(rows: list[tuple]) -> bytes:
    header = b"vendor_name,vendor_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,total_value\n"
    body = b"".join(
        f"{name},{gstin},{inv},{d},{tv},{cgst},{sgst},{igst},{tv + cgst + sgst}\n".encode()
        for name, gstin, inv, d, tv, cgst, sgst, igst in rows
    )
    return header + body


@pytest.fixture
def firm(client, db):
    signup = client.post("/api/auth/signup", json={
        "firm_name": "Portfolio Firm", "full_name": "Pf Owner",
        "email": "owner@portfolio-ca.in", "password": "a-long-enough-password",
    })
    assert signup.status_code == 201, signup.text
    owner_headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    firm_id = signup.json()["firm"]["id"]

    pw = hash_password(TEST_PASSWORD)
    manager = models.User(firm_id=firm_id, name="Pf Manager", email="manager@portfolio-ca.in",
                          role=models.UserRole.MANAGER, password_hash=pw, email_verified=True)
    associate = models.User(firm_id=firm_id, name="Pf Associate", email="associate@portfolio-ca.in",
                            role=models.UserRole.ASSOCIATE, password_hash=pw, email_verified=True)
    db.add_all([manager, associate])
    db.commit()
    return {"owner_headers": owner_headers, "firm_id": firm_id}


def _make_client(client, headers, name: str, gstin: str) -> str:
    resp = client.post("/api/clients", json={"name": name, "gstins": [{"gstin": gstin, "is_primary": True}]},
                       headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["gstins"][0]["id"]


VENDOR = "29AAGCB7383J1Z4"


@pytest.fixture
def portfolio_scenario(client, login, firm):
    h = firm["owner_headers"]

    # A. Needs review: one invoice matches, one is not in GSTR-2B.
    gstin_a = _make_client(client, h, "Alpha Needs Review Ltd", "27AAPFU0939F1ZV")
    client.post(f"/api/gstins/{gstin_a}/uploads/purchase-register?period={PERIOD}",
               files={"file": ("pr.csv", _csv([
                   ("Filed Vendor", VENDOR, "INV-A1", "2026-05-05", 10000, 900, 900, 0),
                   ("Unfiled Vendor", "27AAPFU0939F1ZV", "INV-A2", "2026-05-06", 50000, 4500, 4500, 0),
               ]), "text/csv")}, headers=h)
    client.post(f"/api/gstins/{gstin_a}/gstr2b/upload?period={PERIOD}",
               files={"file": ("2b.json", _doc("27AAPFU0939F1ZV", [
                   {"vendor_name": "Filed Vendor", "vendor_gstin": VENDOR, "invoice_number": "INV-A1",
                    "invoice_date": "2026-05-05", "taxable_value": 10000, "cgst": 900, "sgst": 900,
                    "igst": 0, "total_value": 11800, "filing_status": "FILED"},
               ]), "application/json")}, headers=h)
    r = client.post(f"/api/gstins/{gstin_a}/reconcile?period={PERIOD}", headers=h)
    assert r.status_code == 200, r.text

    # B. Clean: everything matches exactly.
    gstin_b = _make_client(client, h, "Bravo Clean Pvt Ltd", "07AAACH1925Q1ZC")
    client.post(f"/api/gstins/{gstin_b}/uploads/purchase-register?period={PERIOD}",
               files={"file": ("pr.csv", _csv([
                   ("Clean Vendor", VENDOR, "INV-B1", "2026-05-05", 5000, 450, 450, 0),
               ]), "text/csv")}, headers=h)
    client.post(f"/api/gstins/{gstin_b}/gstr2b/upload?period={PERIOD}",
               files={"file": ("2b.json", _doc("07AAACH1925Q1ZC", [
                   {"vendor_name": "Clean Vendor", "vendor_gstin": VENDOR, "invoice_number": "INV-B1",
                    "invoice_date": "2026-05-05", "taxable_value": 5000, "cgst": 450, "sgst": 450,
                    "igst": 0, "total_value": 5900, "filing_status": "FILED"},
               ]), "application/json")}, headers=h)
    r = client.post(f"/api/gstins/{gstin_b}/reconcile?period={PERIOD}", headers=h)
    assert r.status_code == 200, r.text

    # C. Data uploaded, not yet reconciled.
    gstin_c = _make_client(client, h, "Charlie Pending Data Co", "19AABCU9603R1ZK")
    client.post(f"/api/gstins/{gstin_c}/uploads/purchase-register?period={PERIOD}",
               files={"file": ("pr.csv", _csv([
                   ("Any Vendor", VENDOR, "INV-C1", "2026-05-05", 1000, 90, 90, 0),
               ]), "text/csv")}, headers=h)

    # D. Nothing at all yet.
    _make_client(client, h, "Delta Nothing Yet LLP", "24AAACC1206D1ZM")

    # E. Reconciled, awaiting a manager's approval.
    gstin_e = _make_client(client, h, "Echo Awaiting Approval Inc", "33AABCU9702H1ZF")
    client.post(f"/api/gstins/{gstin_e}/uploads/purchase-register?period={PERIOD}",
               files={"file": ("pr.csv", _csv([
                   ("Filed Vendor", VENDOR, "INV-E1", "2026-05-05", 10000, 900, 900, 0),
                   ("Unfiled Vendor", "33AABCU9702H1ZF", "INV-E2", "2026-05-06", 2000, 180, 180, 0),
               ]), "text/csv")}, headers=h)
    client.post(f"/api/gstins/{gstin_e}/gstr2b/upload?period={PERIOD}",
               files={"file": ("2b.json", _doc("33AABCU9702H1ZF", [
                   {"vendor_name": "Filed Vendor", "vendor_gstin": VENDOR, "invoice_number": "INV-E1",
                    "invoice_date": "2026-05-05", "taxable_value": 10000, "cgst": 900, "sgst": 900,
                    "igst": 0, "total_value": 11800, "filing_status": "FILED"},
               ]), "application/json")}, headers=h)
    r = client.post(f"/api/gstins/{gstin_e}/reconcile?period={PERIOD}", headers=h)
    assert r.status_code == 200, r.text
    assoc = login("associate@portfolio-ca.in")
    exc_id = next(e["id"] for e in client.get("/api/exceptions", headers=assoc).json()
                 if e["client_gstin_id"] == gstin_e)
    r = client.patch(f"/api/exceptions/{exc_id}/resolve", json={"resolution_notes": "Reviewed."},
                     headers=assoc)
    assert r.status_code == 200, r.text

    return {"owner_headers": h, "gstin_a": gstin_a, "gstin_b": gstin_b, "gstin_c": gstin_c,
            "gstin_e": gstin_e}


def _by_gstin(rows, gstin_id):
    return next(r for r in rows if r["gstin_id"] == gstin_id)


def test_each_client_lands_in_the_right_bucket(client, portfolio_scenario):
    s = portfolio_scenario
    resp = client.get(f"/api/portfolio?period={PERIOD}", headers=s["owner_headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["period"] == PERIOD
    assert body["total_clients"] == 5
    assert body["needs_review"] == 1
    assert body["awaiting_approval"] == 1
    assert body["needs_data"] == 1
    assert body["not_started"] == 1
    assert body["clean"] == 1

    rows = body["rows"]
    a = _by_gstin(rows, s["gstin_a"])
    assert a["status"] == "NEEDS_REVIEW"
    assert a["exceptions_open"] == 1
    # cgst 4500 + sgst 4500 on the unfiled invoice: tax, not its 50000 taxable value.
    assert a["itc_at_risk"] == "9000.00"

    b = _by_gstin(rows, s["gstin_b"])
    assert b["status"] == "CLEAN" and b["exceptions_open"] == 0 and b["itc_at_risk"] == "0.00"

    c = _by_gstin(rows, s["gstin_c"])
    assert c["status"] == "NEEDS_DATA" and c["run_version"] is None and c["has_purchase_register"] is True

    d = next(r for r in rows if r["client_name"] == "Delta Nothing Yet LLP")
    assert d["status"] == "NOT_STARTED" and d["has_purchase_register"] is False

    e = _by_gstin(rows, s["gstin_e"])
    assert e["status"] == "AWAITING_APPROVAL" and e["exceptions_pending_approval"] == 1
    assert e["exceptions_open"] == 0


def test_rows_are_sorted_worst_first(client, portfolio_scenario):
    s = portfolio_scenario
    rows = client.get(f"/api/portfolio?period={PERIOD}", headers=s["owner_headers"]).json()["rows"]
    order = [r["status"] for r in rows]
    # Every NEEDS_REVIEW precedes every AWAITING_APPROVAL, which precedes
    # NEEDS_DATA, which precedes NOT_STARTED, which precedes CLEAN.
    rank = {"NEEDS_REVIEW": 0, "AWAITING_APPROVAL": 1, "NEEDS_DATA": 2, "NOT_STARTED": 3, "CLEAN": 4}
    assert [rank[s] for s in order] == sorted(rank[s] for s in order)
    assert order[0] == "NEEDS_REVIEW" and order[-1] == "CLEAN"


def test_an_approved_exception_no_longer_counts_as_at_risk(client, login, portfolio_scenario):
    """A partner signing off on an exception is a decision, not a mystery —
    it must drop out of the "credit at risk" figure and the row must go
    CLEAN, the same rule the per-client dashboard applies."""
    s = portfolio_scenario
    associate = login("associate@portfolio-ca.in")
    manager = login("manager@portfolio-ca.in")

    exc_id = next(e["id"] for e in client.get("/api/exceptions", headers=associate).json()
                 if e["client_gstin_id"] == s["gstin_a"])
    r = client.patch(f"/api/exceptions/{exc_id}/resolve",
                     json={"resolution_notes": "Supplier confirmed late filing."}, headers=associate)
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/exceptions/{exc_id}/approve", json={"approved": True}, headers=manager)
    assert r.status_code == 200, r.text

    rows = client.get(f"/api/portfolio?period={PERIOD}", headers=s["owner_headers"]).json()["rows"]
    a = _by_gstin(rows, s["gstin_a"])
    assert a["status"] == "CLEAN"
    assert a["exceptions_open"] == 0 and a["exceptions_pending_approval"] == 0
    assert a["itc_at_risk"] == "0.00"


def test_total_itc_at_risk_sums_across_the_firm(client, portfolio_scenario):
    s = portfolio_scenario
    body = client.get(f"/api/portfolio?period={PERIOD}", headers=s["owner_headers"]).json()
    # Client A's unfiled invoice: cgst 4500 + sgst 4500 = 9000 tax.
    # Client E's unfiled invoice: cgst 180 + sgst 180 = 360 tax, still counted
    # even though it moved to PENDING_APPROVAL — nobody has signed off yet.
    assert float(body["total_itc_at_risk"]) == pytest.approx(9000.0 + 360.0)


def test_a_period_with_no_data_at_all_is_five_not_started_clients(client, portfolio_scenario):
    s = portfolio_scenario
    body = client.get("/api/portfolio?period=1999-01", headers=s["owner_headers"]).json()
    assert body["total_clients"] == 5 and body["not_started"] == 5 and body["needs_review"] == 0
    assert float(body["total_itc_at_risk"]) == 0.0


def test_period_defaults_to_the_latest_gstr2b_period(client, portfolio_scenario):
    from app.services.gstr2b_sync import previous_period

    s = portfolio_scenario
    body = client.get("/api/portfolio", headers=s["owner_headers"]).json()
    assert body["period"] == previous_period()


def test_periods_endpoint_lists_what_has_been_reconciled(client, portfolio_scenario):
    s = portfolio_scenario
    body = client.get("/api/portfolio/periods", headers=s["owner_headers"]).json()
    assert PERIOD in body["periods"]


def test_a_client_viewer_cannot_see_the_portfolio(client, login, db, portfolio_scenario):
    """The portfolio names every client in the firm, and a client-viewer
    account is scoped to exactly one of them — showing it this page would
    leak every other client's name, GSTIN and ITC exposure to an outside
    login. Staff only, same as notices."""
    s = portfolio_scenario
    owner_row = db.query(models.User).filter(models.User.email == "owner@portfolio-ca.in").one()
    viewer = models.User(firm_id=owner_row.firm_id, name="Pf Viewer", email="viewer@portfolio-ca.in",
                         role=models.UserRole.CLIENT_VIEWER, scoped_client_id=None,
                         password_hash=hash_password(TEST_PASSWORD), email_verified=True)
    db.add(viewer)
    db.commit()
    h = login("viewer@portfolio-ca.in")
    resp = client.get(f"/api/portfolio?period={PERIOD}", headers=h)
    assert resp.status_code == 403
    assert client.get("/api/portfolio/periods", headers=h).status_code == 403


def test_unauthenticated_request_is_rejected(client):
    resp = client.get(f"/api/portfolio?period={PERIOD}")
    assert resp.status_code == 401


def test_a_deleted_client_stops_appearing_in_the_portfolio(client, portfolio_scenario):
    """Someone who removed a client should not keep being nagged that it
    'needs data' every month."""
    s = portfolio_scenario
    before = client.get(f"/api/portfolio?period={PERIOD}", headers=s["owner_headers"]).json()
    assert before["total_clients"] == 5

    client_id = _by_gstin(before["rows"], s["gstin_c"])["client_id"]
    r = client.delete(f"/api/clients/{client_id}", headers=s["owner_headers"])
    assert r.status_code == 200, r.text

    after = client.get(f"/api/portfolio?period={PERIOD}", headers=s["owner_headers"]).json()
    assert after["total_clients"] == 4
    assert all(row["gstin_id"] != s["gstin_c"] for row in after["rows"])
