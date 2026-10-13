from app import models
from app.security import hash_password


def test_statutory_dues_keeps_gstr2b_context_distinct_and_requires_independent_review(
    client, login, two_firms, db,
):
    firm = two_firms["a"]
    partner = models.User(
        firm_id=firm["firm"].id, name="Statutory Reviewer", email="statutory-reviewer@alpha-ca.in",
        role=models.UserRole.PARTNER, password_hash=hash_password("test-password-123"),
        email_verified=True,
    )
    db.add(partner)
    db.flush()
    engagement = models.AuditEngagement(
        firm_id=firm["firm"].id, client_id=firm["client"].id,
        financial_year="2025-26", engagement_type=models.AuditEngagementType.STATUTORY,
        engagement_partner_id=partner.id, created_by=firm["users"]["manager"].id,
    )
    db.add(engagement)
    db.flush()
    gstin = firm["gstin"]
    db.add(models.GSTR2BFetch(
        firm_id=firm["firm"].id, client_gstin_id=gstin.id, period="2025-04",
        version=1, is_current=True, source=models.FetchSource.SAMPLE,
    ))
    db.commit()

    associate_headers = login("associate@alpha-ca.in")
    manager_headers = login("manager@alpha-ca.in")
    before = client.get(f"/api/audit-engagements/{engagement.id}/statutory-dues", headers=associate_headers)
    assert before.status_code == 200, before.text
    assert len(before.json()) == 48
    gst_april = next(row for row in before.json() if row["period"] == "2025-04" and row["tax_type"] == "GST")
    assert gst_april["filing_status"] is None
    assert gst_april["verification_status"] == "NOT_RECORDED"
    assert gst_april["gstr2b_context"][0]["gstr2b_available"] is True
    assert gst_april["gstr2b_context"][0]["reconciliation_available"] is False

    recorded = client.post(f"/api/audit-engagements/{engagement.id}/statutory-dues", headers=associate_headers, json={
        "tax_type": "GST", "period": "2025-04", "filing_status": "ARREARS",
        "filing_source": "PORTAL_RECORD", "statutory_due_date": "2025-05-20",
        "amount_due": "25000.00", "amount_paid": "10000.00",
        "source_reference": "Portal challan and return review",
    })
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["reviewed_by"] is None

    flags = client.get(f"/api/audit-engagements/{engagement.id}/risk-flags?status=SYSTEM_FLAGGED", headers=associate_headers)
    assert flags.status_code == 200
    assert len(flags.json()) == 1
    assert flags.json()[0]["flag_type"] == "STATUTORY_DUES"
    assert "not a CARO conclusion" in flags.json()[0]["system_note"]

    self_review = client.post(
        f"/api/audit-engagements/{engagement.id}/statutory-dues/{recorded.json()['id']}/review",
        headers=associate_headers,
    )
    assert self_review.status_code == 403
    reviewed = client.post(
        f"/api/audit-engagements/{engagement.id}/statutory-dues/{recorded.json()['id']}/review",
        headers=login(partner.email),
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["reviewed_by"] == partner.id

    after = client.get(f"/api/audit-engagements/{engagement.id}/statutory-dues", headers=manager_headers)
    row = next(item for item in after.json() if item["period"] == "2025-04" and item["tax_type"] == "GST")
    assert row["filing_status"] == "ARREARS"
    assert row["verification_status"] == "REVIEWED"


def test_statutory_filing_period_must_be_in_engagement_year(client, login, two_firms, db):
    firm = two_firms["a"]
    partner = models.User(
        firm_id=firm["firm"].id, name="Partner", email="fy-partner@alpha-ca.in",
        role=models.UserRole.PARTNER, password_hash=hash_password("test-password-123"), email_verified=True,
    )
    db.add(partner)
    db.flush()
    engagement = models.AuditEngagement(
        firm_id=firm["firm"].id, client_id=firm["client"].id,
        financial_year="2025-26", engagement_type=models.AuditEngagementType.STATUTORY,
        engagement_partner_id=partner.id, created_by=firm["users"]["manager"].id,
    )
    db.add(engagement)
    db.commit()
    response = client.post(f"/api/audit-engagements/{engagement.id}/statutory-dues", headers=login("associate@alpha-ca.in"), json={
        "tax_type": "PF", "period": "2025-03", "filing_status": "PENDING",
        "filing_source": "CLIENT_RECORD", "statutory_due_date": "2025-04-15", "notes": "Client schedule",
    })
    assert response.status_code == 422
