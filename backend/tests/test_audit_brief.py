"""
The pre-audit brief: a client's GST compliance health computed from real
reconciliation history, over several months, built through the actual upload
and reconcile endpoints so this exercises the same data the app would hold.

Scenario across four months for one client, ending 2026-05:
  2026-02  Dominant Vendor matches; Small Vendor matches. Reconciled.
  2026-03  Dominant Vendor matches; Small Vendor is not filed (exception). Reconciled.
  2026-04  Nothing uploaded at all — a filing-consistency gap.
  2026-05  Dominant Vendor matches; Small Vendor is not filed again (exception),
           and one invoice carries blocked GSTR-2B credit. Reconciled.

This is deliberately built to trip every red flag at once: vendor
concentration (Dominant Vendor is ~89% of claimed ITC), a recurring offender
(Small Vendor, flagged in two of the four months), a filing gap (one month
with no reconciliation), and blocked credit — while the "three periods rising"
trend flag should NOT fire, since risk does not rise in three straight
reconciled periods here. That absence is asserted explicitly.
"""
import json
from datetime import timedelta

import pytest

from app import models
from app.security import hash_password
from tests.conftest import TEST_PASSWORD
from app.services.audit_brief import periods_ending

DOMINANT = "29AAGCB7383J1Z4"   # ~89% of ITC
SMALL = "07AAACH1925Q1ZC"      # the recurring offender


def _doc(gstin: str, period: str, invoices: list) -> bytes:
    return json.dumps({"gstin": gstin, "period": period, "generated_on": "2026-01-14",
                       "invoices": invoices}).encode()


def _csv(rows: list[tuple]) -> bytes:
    header = b"vendor_name,vendor_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,total_value\n"
    body = b"".join(
        f"{name},{gstin},{inv},{d},{tv},{cgst},{sgst},{igst},{tv + cgst + sgst}\n".encode()
        for name, gstin, inv, d, tv, cgst, sgst, igst in rows
    )
    return header + body


# ---------------------------------------------------------------------------
# periods_ending — a pure function, worth checking on its own
# ---------------------------------------------------------------------------

def test_periods_ending_walks_backward_including_as_of():
    assert periods_ending("2026-05", 4) == ["2026-02", "2026-03", "2026-04", "2026-05"]


def test_periods_ending_crosses_a_year_boundary():
    assert periods_ending("2026-02", 4) == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_periods_ending_of_one_is_just_that_period():
    assert periods_ending("2026-05", 1) == ["2026-05"]


# ---------------------------------------------------------------------------
# The full scenario, over HTTP
# ---------------------------------------------------------------------------

CLIENT_GSTIN = "27AAPFU0939F1ZV"


@pytest.fixture
def scenario(client, login, db):
    signup = client.post("/api/auth/signup", json={
        "firm_name": "Brief Firm", "full_name": "Br Owner",
        "email": "owner@brief-ca.in", "password": "a-long-enough-password",
    })
    assert signup.status_code == 201, signup.text
    h = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    firm_id = signup.json()["firm"]["id"]

    created = client.post("/api/clients", json={
        "name": "Audit Ready Co", "gstins": [{"gstin": CLIENT_GSTIN, "is_primary": True}],
    }, headers=h)
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]
    gstin_id = created.json()["gstins"][0]["id"]

    def reconcile(period: str, pr_rows, b2_invoices):
        r = client.post(f"/api/gstins/{gstin_id}/uploads/purchase-register?period={period}",
                        files={"file": ("pr.csv", _csv(pr_rows), "text/csv")}, headers=h)
        assert r.status_code == 200, r.text
        r = client.post(f"/api/gstins/{gstin_id}/gstr2b/upload?period={period}",
                        files={"file": ("2b.json", _doc(CLIENT_GSTIN, period, b2_invoices), "application/json")},
                        headers=h)
        assert r.status_code == 200, r.text
        r = client.post(f"/api/gstins/{gstin_id}/reconcile?period={period}", headers=h)
        assert r.status_code == 200, r.text

    # 2026-02: everything matches.
    reconcile("2026-02", [
        ("Dominant Vendor", DOMINANT, "INV-D1", "2026-02-05", 40000, 3600, 3600, 0),
        ("Small Vendor", SMALL, "INV-S1", "2026-02-06", 5000, 450, 450, 0),
    ], [
        {"vendor_name": "Dominant Vendor", "vendor_gstin": DOMINANT, "invoice_number": "INV-D1",
         "invoice_date": "2026-02-05", "taxable_value": 40000, "cgst": 3600, "sgst": 3600,
         "igst": 0, "total_value": 47200, "filing_status": "FILED"},
        {"vendor_name": "Small Vendor", "vendor_gstin": SMALL, "invoice_number": "INV-S1",
         "invoice_date": "2026-02-06", "taxable_value": 5000, "cgst": 450, "sgst": 450,
         "igst": 0, "total_value": 5900, "filing_status": "FILED"},
    ])

    # 2026-03: Small Vendor is not filed.
    reconcile("2026-03", [
        ("Dominant Vendor", DOMINANT, "INV-D2", "2026-03-05", 40000, 3600, 3600, 0),
        ("Small Vendor", SMALL, "INV-S2", "2026-03-06", 5000, 450, 450, 0),
    ], [
        {"vendor_name": "Dominant Vendor", "vendor_gstin": DOMINANT, "invoice_number": "INV-D2",
         "invoice_date": "2026-03-05", "taxable_value": 40000, "cgst": 3600, "sgst": 3600,
         "igst": 0, "total_value": 47200, "filing_status": "FILED"},
    ])

    # 2026-04: nothing uploaded — a gap in the window.

    # 2026-05: Small Vendor unfiled again, plus a blocked-credit invoice.
    reconcile("2026-05", [
        ("Dominant Vendor", DOMINANT, "INV-D3", "2026-05-05", 40000, 3600, 3600, 0),
        ("Small Vendor", SMALL, "INV-S3", "2026-05-06", 5000, 450, 450, 0),
    ], [
        {"vendor_name": "Dominant Vendor", "vendor_gstin": DOMINANT, "invoice_number": "INV-D3",
         "invoice_date": "2026-05-05", "taxable_value": 40000, "cgst": 3600, "sgst": 3600,
         "igst": 0, "total_value": 47200, "filing_status": "FILED"},
        {"vendor_name": "Blocked Vendor", "vendor_gstin": DOMINANT, "invoice_number": "INV-BLOCK",
         "invoice_date": "2026-05-07", "taxable_value": 1000, "cgst": 90, "sgst": 90,
         "igst": 0, "total_value": 1180, "filing_status": "FILED", "itc_available": False},
    ])

    # Backdate the 2026-03 exception so it shows up as a stale exception too.
    old_exc = (
        db.query(models.ReconciliationException)
        .filter(models.ReconciliationException.client_gstin_id == gstin_id,
                models.ReconciliationException.period == "2026-03")
        .one()
    )
    old_exc.created_at = old_exc.created_at - timedelta(days=200)
    db.commit()

    return {"owner_headers": h, "client_id": client_id, "gstin_id": gstin_id, "firm_id": firm_id}


def test_the_brief_computes_vendor_concentration(client, scenario):
    s = scenario
    resp = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=2026-05&lookback=4",
                      headers=s["owner_headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["as_of"] == "2026-05"
    assert body["lookback_periods"] == ["2026-02", "2026-03", "2026-04", "2026-05"]
    top = body["vendor_concentration"][0]
    assert top["vendor_gstin"] == DOMINANT
    assert top["share"] == pytest.approx(88.9, abs=0.2)


def test_the_brief_finds_the_recurring_offender(client, scenario):
    s = scenario
    body = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=2026-05&lookback=4",
                      headers=s["owner_headers"]).json()
    recurring = {v["vendor_gstin"]: v for v in body["recurring_vendors"]}
    assert SMALL in recurring
    assert recurring[SMALL]["periods_flagged"] == ["2026-03", "2026-05"]
    assert DOMINANT not in recurring


def test_the_brief_reports_a_filing_gap(client, scenario):
    s = scenario
    body = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=2026-05&lookback=4",
                      headers=s["owner_headers"]).json()
    assert body["reconciled_periods"] == 3 and body["total_periods"] == 4
    assert body["filing_consistency"] == 75.0
    gap_point = next(p for p in body["points"] if p["period"] == "2026-04")
    assert gap_point["reconciled"] is False and gap_point["invoice_count"] == 0


def test_the_brief_surfaces_blocked_credit(client, scenario):
    s = scenario
    body = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=2026-05&lookback=4",
                      headers=s["owner_headers"]).json()
    may = next(p for p in body["points"] if p["period"] == "2026-05")
    assert may["itc_blocked"] == "180.00"
    assert any("17(5)" in f["message"] for f in body["red_flags"])


def test_the_brief_finds_the_stale_exception(client, scenario):
    s = scenario
    body = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=2026-05&lookback=4",
                      headers=s["owner_headers"]).json()
    assert len(body["stale_exceptions"]) == 1
    stale = body["stale_exceptions"][0]
    assert stale["period"] == "2026-03" and stale["age_days"] >= 200
    assert any("open for more than" in f["message"] for f in body["red_flags"])


def test_an_approved_exception_stops_counting_as_at_risk(client, login, db, scenario):
    """Signing off on an exception is a decision, not a mystery — it must
    drop out of the "ITC at risk" figure for its period, the same rule the
    per-client dashboard and the portfolio both apply."""
    s = scenario
    manager = models.User(firm_id=s["firm_id"], name="Br Manager", email="manager@brief-ca.in",
                          role=models.UserRole.MANAGER, password_hash=hash_password(TEST_PASSWORD),
                          email_verified=True)
    db.add(manager)
    db.commit()

    before = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=2026-05&lookback=4",
                        headers=s["owner_headers"]).json()
    before_march = next(p for p in before["points"] if p["period"] == "2026-03")
    # Small Vendor's unfiled invoice: cgst 450 + sgst 450 = 900 tax.
    assert before_march["itc_at_risk"] == "900.00"

    exc_id = next(e["id"] for e in client.get("/api/exceptions", headers=s["owner_headers"]).json()
                 if e["period"] == "2026-03")
    r = client.patch(f"/api/exceptions/{exc_id}/resolve",
                     json={"resolution_notes": "Confirmed a late filer, immaterial."},
                     headers=s["owner_headers"])
    assert r.status_code == 200, r.text
    manager_headers = login("manager@brief-ca.in")
    r = client.patch(f"/api/exceptions/{exc_id}/approve", json={"approved": True}, headers=manager_headers)
    assert r.status_code == 200, r.text

    after = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=2026-05&lookback=4",
                       headers=s["owner_headers"]).json()
    after_march = next(p for p in after["points"] if p["period"] == "2026-03")
    assert after_march["itc_at_risk"] == "0.00"


def test_red_flags_cover_concentration_and_filing_gap_but_not_a_false_trend(client, scenario):
    """Risk does not rise for three straight reconciled periods here (it goes
    0 -> 450 -> 450), so the trend flag must NOT fire — a brief that cries
    wolf stops being read."""
    s = scenario
    body = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=2026-05&lookback=4",
                      headers=s["owner_headers"]).json()
    messages = " | ".join(f["message"] for f in body["red_flags"])
    assert "concentration risk" in messages
    assert "no completed reconciliation" in messages
    assert "consecutive reconciled periods" not in messages


def test_an_actual_three_period_rise_is_flagged(client, login, db):
    """A separate, minimal scenario where risk genuinely rises for three
    straight reconciled months, to prove the trend detector fires when it
    should, not only that it stays quiet when it shouldn't."""
    signup = client.post("/api/auth/signup", json={
        "firm_name": "Rising Firm", "full_name": "Rf Owner",
        "email": "owner@rising-ca.in", "password": "a-long-enough-password",
    })
    h = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    created = client.post("/api/clients", json={
        "name": "Worsening Co", "gstins": [{"gstin": CLIENT_GSTIN, "is_primary": True}],
    }, headers=h)
    client_id = created.json()["id"]
    gstin_id = created.json()["gstins"][0]["id"]

    for period, amount in [("2026-02", 1000), ("2026-03", 2000), ("2026-04", 3000)]:
        cgst = amount // 10
        r = client.post(f"/api/gstins/{gstin_id}/uploads/purchase-register?period={period}",
                        files={"file": ("pr.csv", _csv([
                            ("Worsening Vendor", DOMINANT, f"INV-{period}", f"{period}-05",
                             amount, cgst, cgst, 0),
                        ]), "text/csv")}, headers=h)
        assert r.status_code == 200, r.text
        # No GSTR-2B at all: every invoice is SUPPLIER_NOT_FILED and at risk.
        r = client.post(f"/api/gstins/{gstin_id}/reconcile?period={period}", headers=h)
        assert r.status_code == 200, r.text

    body = client.get(f"/api/clients/{client_id}/audit-brief?as_of=2026-04&lookback=3",
                      headers=h).json()
    assert any("consecutive reconciled periods" in f["message"] for f in body["red_flags"])


def test_a_client_with_no_history_gets_an_empty_honest_brief(client, login, db):
    signup = client.post("/api/auth/signup", json={
        "firm_name": "Empty Firm", "full_name": "Ef Owner",
        "email": "owner@empty-ca.in", "password": "a-long-enough-password",
    })
    h = {"Authorization": f"Bearer {signup.json()['access_token']}"}
    created = client.post("/api/clients", json={
        "name": "Brand New Co", "gstins": [{"gstin": CLIENT_GSTIN, "is_primary": True}],
    }, headers=h)
    client_id = created.json()["id"]

    resp = client.get(f"/api/clients/{client_id}/audit-brief", headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reconciled_periods"] == 0
    assert body["vendor_concentration"] == [] and body["recurring_vendors"] == []
    assert body["red_flags"]      # a client with zero reconciliation history is itself a flag


def test_scope_note_is_always_present_and_honest(client, scenario):
    s = scenario
    body = client.get(f"/api/clients/{s['client_id']}/audit-brief", headers=s["owner_headers"]).json()
    assert "general ledger" in body["scope_note"] or "trial balance" in body["scope_note"]


def test_an_invalid_period_is_a_clear_422(client, scenario):
    s = scenario
    resp = client.get(f"/api/clients/{s['client_id']}/audit-brief?as_of=not-a-period",
                      headers=s["owner_headers"])
    assert resp.status_code == 422


def test_a_client_viewer_cannot_see_the_brief(client, login, db, scenario):
    s = scenario
    viewer = models.User(firm_id=s["firm_id"], name="Br Viewer", email="viewer@brief-ca.in",
                         role=models.UserRole.CLIENT_VIEWER, scoped_client_id=s["client_id"],
                         password_hash=hash_password(TEST_PASSWORD), email_verified=True)
    db.add(viewer)
    db.commit()
    h = login("viewer@brief-ca.in")
    resp = client.get(f"/api/clients/{s['client_id']}/audit-brief", headers=h)
    assert resp.status_code == 403


def test_a_nonexistent_client_is_404(client, scenario):
    s = scenario
    resp = client.get("/api/clients/no-such-client/audit-brief", headers=s["owner_headers"])
    assert resp.status_code == 404
