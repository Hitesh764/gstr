"""
End-to-end workflow: upload, reconcile, resolve, approve, re-reconcile.

The test that matters most here is `test_resolution_survives_rereconciliation`.
In the original single-tenant MVP, re-running a reconciliation deleted and
recreated every result row, silently destroying the resolution notes an
associate had written. That is the regression this suite guards against.
"""
from datetime import date

import pytest

from app import models
from tests.conftest import TEST_PASSWORD

PERIOD = "2026-03"

# Two invoices: one will match GSTR-2B exactly, one will not be filed.
REGISTER_CSV = (
    b"vendor_name,vendor_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,total_value\n"
    b"Filed Vendor,29AAGCB7383J1Z4,INV-100,2026-03-05,10000,900,900,0,11800\n"
    b"Unfiled Vendor,27AAPFU0939F1ZV,INV-200,2026-03-06,50000,4500,4500,0,59000\n"
)

GSTR2B_DOC = {
    "gstin": "27AAPFU0939F1ZV",
    "period": PERIOD,
    "generated_on": "2026-04-14",
    "invoices": [
        {
            "vendor_name": "Filed Vendor", "vendor_gstin": "29AAGCB7383J1Z4",
            "invoice_number": "INV-100", "invoice_date": "2026-03-05",
            "taxable_value": 10000, "cgst": 900, "sgst": 900, "igst": 0,
            "total_value": 11800, "filing_status": "FILED",
        },
    ],
}


@pytest.fixture
def firm_with_gstin(client, db):
    """A firm created through the API, with a client and GSTIN."""
    import json

    signup = client.post("/api/auth/signup", json={
        "firm_name": "Workflow Firm", "full_name": "Wf Owner",
        "email": "owner@workflow-ca.in", "password": "a-long-enough-password",
    })
    assert signup.status_code == 201, signup.text
    owner_headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    # Add a manager and an associate via direct model creation, so the
    # approval path has two distinct people.
    from app.security import hash_password
    firm_id = signup.json()["firm"]["id"]
    pw = hash_password(TEST_PASSWORD)
    manager = models.User(firm_id=firm_id, name="Wf Manager",
                          email="manager@workflow-ca.in", role=models.UserRole.MANAGER,
                          password_hash=pw, email_verified=True)
    associate = models.User(firm_id=firm_id, name="Wf Associate",
                            email="associate@workflow-ca.in",
                            role=models.UserRole.ASSOCIATE,
                            password_hash=pw, email_verified=True)
    db.add_all([manager, associate])
    db.commit()

    created = client.post("/api/clients", json={
        "name": "Workflow Client Ltd",
        "gstins": [{"gstin": "27AAPFU0939F1ZV", "is_primary": True}],
    }, headers=owner_headers)
    assert created.status_code == 201, created.text
    gstin_id = created.json()["gstins"][0]["id"]

    return {"owner_headers": owner_headers, "gstin_id": gstin_id,
            "client_id": created.json()["id"], "gstr2b": json.dumps(GSTR2B_DOC).encode()}


def _upload_and_reconcile(client, headers, gstin_id, gstr2b_bytes, csv=REGISTER_CSV):
    up = client.post(
        f"/api/gstins/{gstin_id}/uploads/purchase-register?period={PERIOD}",
        files={"file": ("pr.csv", csv, "text/csv")}, headers=headers,
    )
    assert up.status_code == 200, up.text
    assert up.json()["accepted"] is True

    fetch = client.post(
        f"/api/gstins/{gstin_id}/gstr2b/upload?period={PERIOD}",
        files={"file": ("2b.json", gstr2b_bytes, "application/json")}, headers=headers,
    )
    assert fetch.status_code == 200, fetch.text

    run = client.post(f"/api/gstins/{gstin_id}/reconcile?period={PERIOD}", headers=headers)
    assert run.status_code == 200, run.text
    return run.json()


def test_full_reconciliation_flow(client, firm_with_gstin):
    f = firm_with_gstin
    summary = _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    assert summary["counts_by_status"]["MATCHED"] == 1
    assert summary["counts_by_status"]["SUPPLIER_NOT_FILED"] == 1
    # ITC is the tax (cgst+sgst+igst), not the taxable value: 1800 + 9000.
    assert summary["total_itc_in_books"] == 10800.0
    assert summary["total_itc_eligible_2b"] == 1800.0
    assert summary["total_itc_blocked_2b"] == 0.0
    # The unfiled invoice's tax is the credit at risk.
    assert summary["total_itc_at_risk"] == 9000.0
    assert summary["pct_auto_matched"] == 50.0


def test_exception_queue_and_lapse_deadline(client, firm_with_gstin):
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    resp = client.get("/api/exceptions", headers=f["owner_headers"])
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1

    exc = items[0]
    assert exc["status"] == "OPEN"
    # March 2026 falls in FY 2025-26, so the credit lapses on 30 Nov 2026.
    assert exc["itc_lapse_deadline"] == "2026-11-30"
    assert exc["urgency"] in ("ok", "warning", "critical", "lapsed")


def test_upload_creates_new_version_not_overwrite(client, firm_with_gstin):
    f = firm_with_gstin
    headers = f["owner_headers"]
    _upload_and_reconcile(client, headers, f["gstin_id"], f["gstr2b"])

    # Re-upload a corrected register: the unfiled invoice's value changes.
    corrected = REGISTER_CSV.replace(b"50000,4500,4500,0,59000", b"45000,4050,4050,0,53100")
    up = client.post(
        f"/api/gstins/{f['gstin_id']}/uploads/purchase-register?period={PERIOD}",
        files={"file": ("pr_v2.csv", corrected, "text/csv")}, headers=headers,
    )
    assert up.status_code == 200
    assert up.json()["upload"]["version"] == 2

    versions = client.get(f"/api/gstins/{f['gstin_id']}/uploads", headers=headers).json()
    assert len(versions) == 2, "the first upload must be retained, not overwritten"
    assert {v["version"] for v in versions} == {1, 2}
    assert sum(1 for v in versions if v["is_current"]) == 1


def test_upload_diff_reports_changes(client, firm_with_gstin):
    f = firm_with_gstin
    headers = f["owner_headers"]
    _upload_and_reconcile(client, headers, f["gstin_id"], f["gstr2b"])

    corrected = REGISTER_CSV.replace(b"50000,4500,4500,0,59000", b"45000,4050,4050,0,53100")
    up = client.post(
        f"/api/gstins/{f['gstin_id']}/uploads/purchase-register?period={PERIOD}",
        files={"file": ("pr_v2.csv", corrected, "text/csv")}, headers=headers,
    )
    upload_id = up.json()["upload"]["id"]

    diff = client.get(f"/api/gstins/{f['gstin_id']}/uploads/{upload_id}/diff",
                      headers=headers).json()
    assert diff["summary"]["changed"] == 1
    assert diff["summary"]["added"] == 0
    assert diff["summary"]["removed"] == 0
    assert diff["changed"][0]["from"] == 50000.0
    assert diff["changed"][0]["to"] == 45000.0


def test_upload_rejects_invalid_rows_without_importing(client, firm_with_gstin):
    f = firm_with_gstin
    bad = (b"vendor_name,vendor_gstin,invoice_number,invoice_date,taxable_value\n"
           b"Bad,27AAPFU0939F1ZX,INV-1,2026-03-05,1000\n")
    resp = client.post(
        f"/api/gstins/{f['gstin_id']}/uploads/purchase-register?period={PERIOD}",
        files={"file": ("bad.csv", bad, "text/csv")}, headers=f["owner_headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert body["inserted"] == 0
    assert body["upload"] is None
    assert any("check digit" in e["message"].lower() for e in body["errors"])


def test_gstr2b_for_wrong_gstin_is_rejected(client, firm_with_gstin):
    import json
    f = firm_with_gstin
    wrong = dict(GSTR2B_DOC, gstin="29AAGCB7383J1Z4")
    resp = client.post(
        f"/api/gstins/{f['gstin_id']}/gstr2b/upload?period={PERIOD}",
        files={"file": ("2b.json", json.dumps(wrong).encode(), "application/json")},
        headers=f["owner_headers"],
    )
    assert resp.status_code == 422
    assert "29AAGCB7383J1Z4" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Approval workflow
# ---------------------------------------------------------------------------

def test_approval_chain(client, login, firm_with_gstin):
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    associate = login("associate@workflow-ca.in")
    manager = login("manager@workflow-ca.in")

    exc_id = client.get("/api/exceptions", headers=associate).json()[0]["id"]

    resolved = client.patch(f"/api/exceptions/{exc_id}/resolve",
                            json={"resolution_notes": "Supplier confirmed filing in April."},
                            headers=associate)
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "PENDING_APPROVAL", (
        "an associate's resolution is a proposal, not an approval"
    )

    approved = client.patch(f"/api/exceptions/{exc_id}/approve",
                            json={"approved": True}, headers=manager)
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"


def test_associate_cannot_approve(client, login, firm_with_gstin):
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    associate = login("associate@workflow-ca.in")
    exc_id = client.get("/api/exceptions", headers=associate).json()[0]["id"]
    client.patch(f"/api/exceptions/{exc_id}/resolve",
                 json={"resolution_notes": "done"}, headers=associate)

    resp = client.patch(f"/api/exceptions/{exc_id}/approve",
                        json={"approved": True}, headers=associate)
    assert resp.status_code == 403


def test_cannot_approve_own_resolution(client, login, firm_with_gstin):
    """Separation of duties, even for someone whose role would allow it."""
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    manager = login("manager@workflow-ca.in")
    exc_id = client.get("/api/exceptions", headers=manager).json()[0]["id"]
    client.patch(f"/api/exceptions/{exc_id}/resolve",
                 json={"resolution_notes": "I did this myself"}, headers=manager)

    resp = client.patch(f"/api/exceptions/{exc_id}/approve",
                        json={"approved": True}, headers=manager)
    assert resp.status_code == 403
    assert "someone else" in resp.json()["detail"]


def test_rework_requires_a_reason(client, login, firm_with_gstin):
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    associate = login("associate@workflow-ca.in")
    manager = login("manager@workflow-ca.in")
    exc_id = client.get("/api/exceptions", headers=associate).json()[0]["id"]
    client.patch(f"/api/exceptions/{exc_id}/resolve",
                 json={"resolution_notes": "done"}, headers=associate)

    resp = client.patch(f"/api/exceptions/{exc_id}/approve",
                        json={"approved": False}, headers=manager)
    assert resp.status_code == 422

    resp = client.patch(f"/api/exceptions/{exc_id}/approve",
                        json={"approved": False, "rework_reason": "Attach the supplier email."},
                        headers=manager)
    assert resp.status_code == 200
    assert resp.json()["status"] == "REWORK"


def test_approved_exception_removes_item_from_at_risk_total(client, login, firm_with_gstin):
    f = firm_with_gstin
    summary = _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])
    assert summary["total_itc_at_risk"] == 9000.0

    associate = login("associate@workflow-ca.in")
    manager = login("manager@workflow-ca.in")
    exc_id = client.get("/api/exceptions", headers=associate).json()[0]["id"]
    client.patch(f"/api/exceptions/{exc_id}/resolve",
                 json={"resolution_notes": "Written off deliberately."}, headers=associate)
    client.patch(f"/api/exceptions/{exc_id}/approve",
                 json={"approved": True}, headers=manager)

    after = client.get(
        f"/api/gstins/{f['gstin_id']}/reconciliation/{PERIOD}/summary",
        headers=f["owner_headers"],
    ).json()
    assert after["total_itc_at_risk"] == 0.0, (
        "an approved resolution should clear the item from the at-risk figure"
    )


def test_resolution_survives_rereconciliation(client, login, firm_with_gstin):
    """The regression this whole redesign exists for.

    Re-running a reconciliation must not destroy the resolution notes and
    approvals a firm has already recorded.
    """
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    associate = login("associate@workflow-ca.in")
    manager = login("manager@workflow-ca.in")

    exc_id = client.get("/api/exceptions", headers=associate).json()[0]["id"]
    notes = "Supplier confirmed GSTR-1 will be filed next period. Tracked."
    client.patch(f"/api/exceptions/{exc_id}/resolve",
                 json={"resolution_notes": notes}, headers=associate)
    client.patch(f"/api/exceptions/{exc_id}/approve",
                 json={"approved": True}, headers=manager)

    # Re-run the reconciliation over unchanged data.
    rerun = client.post(f"/api/gstins/{f['gstin_id']}/reconcile?period={PERIOD}",
                        headers=f["owner_headers"])
    assert rerun.status_code == 200

    items = client.get("/api/exceptions", headers=associate).json()
    assert len(items) == 1, "re-running should not multiply exceptions"
    carried = items[0]
    assert carried["resolution_notes"] == notes, "resolution notes were lost on re-run"
    assert carried["status"] == "APPROVED", "approval was lost on re-run"


def test_bulk_resolve(client, login, firm_with_gstin):
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    associate = login("associate@workflow-ca.in")
    ids = [e["id"] for e in client.get("/api/exceptions", headers=associate).json()]

    resp = client.post("/api/exceptions/bulk/resolve",
                       json={"exception_ids": ids,
                             "resolution_notes": "Batch: all timing differences."},
                       headers=associate)
    assert resp.status_code == 200
    assert resp.json()["resolved"] == len(ids)

    for e in client.get("/api/exceptions", headers=associate).json():
        assert e["status"] == "PENDING_APPROVAL"


def test_comment_thread(client, login, firm_with_gstin):
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    associate = login("associate@workflow-ca.in")
    exc_id = client.get("/api/exceptions", headers=associate).json()[0]["id"]

    posted = client.post(f"/api/exceptions/{exc_id}/comments",
                         json={"comment": "Chased the supplier on 12 April."},
                         headers=associate)
    assert posted.status_code == 201

    thread = client.get(f"/api/exceptions/{exc_id}/comments", headers=associate).json()
    assert len(thread) == 1
    assert thread[0]["comment"] == "Chased the supplier on 12 April."


def test_export_produces_a_spreadsheet(client, firm_with_gstin):
    f = firm_with_gstin
    _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])

    resp = client.get(f"/api/gstins/{f['gstin_id']}/reconciliation/{PERIOD}/export",
                      headers=f["owner_headers"])
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    # XLSX files are ZIP archives; check the magic bytes rather than trusting
    # the content-type header alone.
    assert resp.content[:2] == b"PK"


def test_dashboard_itc_totals_are_tax_not_taxable_value(client, firm_with_gstin):
    """A regression test for a bug where the dashboard's "ITC" figures summed
    taxable_value (the pre-tax invoice amount) instead of the actual tax
    (cgst+sgst+igst). Wrong in a way that looked plausible, which is worse
    than an obvious error: a partner reading "Rs 60,000 ITC" would believe it."""
    f = firm_with_gstin
    summary = _upload_and_reconcile(client, f["owner_headers"], f["gstin_id"], f["gstr2b"])
    # Taxable values in the fixture are 10000 and 50000; if the bug were still
    # present these totals would be 10000/60000/50000 rather than the tax sums.
    assert summary["total_itc_in_books"] not in (60000.0, 10000.0, 50000.0)
    assert summary["total_itc_eligible_2b"] not in (60000.0, 10000.0, 50000.0)
    assert summary["total_itc_at_risk"] not in (60000.0, 10000.0, 50000.0)


def test_blocked_itc_in_gstr2b_is_excluded_from_eligible_and_reported_separately(
        client, firm_with_gstin):
    """An invoice GSTN marks itc_available=false (e.g. blocked credit under
    Section 17(5)) is real GSTR-2B data but is not credit the client can
    claim, so it must not inflate "eligible ITC" — and it should not be
    silently dropped either."""
    import json

    f = firm_with_gstin
    doc = json.loads(f["gstr2b"])
    doc["invoices"].append({
        "vendor_name": "Blocked Vendor", "vendor_gstin": "29AAGCB7383J1Z4",
        "invoice_number": "INV-300", "invoice_date": "2026-03-07",
        "taxable_value": 20000, "cgst": 1800, "sgst": 1800, "igst": 0,
        "total_value": 23600, "filing_status": "FILED", "itc_available": False,
    })
    csv = (
        b"vendor_name,vendor_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,total_value\n"
        b"Filed Vendor,29AAGCB7383J1Z4,INV-100,2026-03-05,10000,900,900,0,11800\n"
        b"Unfiled Vendor,27AAPFU0939F1ZV,INV-200,2026-03-06,50000,4500,4500,0,59000\n"
        b"Blocked Vendor,29AAGCB7383J1Z4,INV-300,2026-03-07,20000,1800,1800,0,23600\n"
    )
    summary = _upload_and_reconcile(
        client, f["owner_headers"], f["gstin_id"], json.dumps(doc).encode(), csv=csv)

    # Eligible ITC is unchanged by the blocked invoice (1800 from Filed Vendor only).
    assert summary["total_itc_eligible_2b"] == 1800.0
    # The blocked invoice's tax (3600) is surfaced, not folded into "eligible".
    assert summary["total_itc_blocked_2b"] == 3600.0
