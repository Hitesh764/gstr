from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app import models
from app.ingestion.audit_ledger import parse_audit_ledger
from app.security import hash_password
from app.services.audit_ageing import bucket_for_age, build_ageing_data


def test_age_bucket_boundaries_are_exclusive_and_deterministic():
    assert [bucket_for_age(value) for value in (0, 30, 31, 60, 61, 90, 91, 180, 181)] == [
        "0-30", "0-30", "31-60", "31-60", "61-90", "61-90", "91-180", "91-180", "180+",
    ]


def test_ageing_uses_due_date_then_invoice_date_and_marks_explanation():
    rows = [
        SimpleNamespace(id="a", account_type=models.AgeingType.DEBTORS, party_name="Alpha",
                        invoice_number="A1", invoice_date=date(2025, 1, 1), due_date=date(2025, 9, 1),
                        open_balance=Decimal("100"), explanation="Payment plan agreed"),
        SimpleNamespace(id="b", account_type=models.AgeingType.DEBTORS, party_name="Beta",
                        invoice_number="B1", invoice_date=date(2026, 3, 10), due_date=None,
                        open_balance=Decimal("200"), explanation=None),
    ]
    result = build_ageing_data(rows, as_of_date=date(2026, 3, 31), account_type="DEBTORS")
    assert result["bucket_totals"]["180+"] == "100.00"
    assert result["bucket_totals"]["0-30"] == "200.00"
    assert result["oldest_bucket_reviews"][0]["review_note"].startswith("Balance is more than 180")
    assert result["oldest_bucket_reviews"][0]["explanation_on_file"] is True
    assert result["professional_review_required"] is True


def test_ledger_parser_rejects_ambiguous_non_iso_dates():
    result = parse_audit_ledger(
        b"account_type,party_name,invoice_number,invoice_date,open_balance\nDEBTORS,Acme,INV-1,03/04/2025,100.00\n",
        "ledger.csv",
    )
    assert len(result.errors) == 1
    assert result.errors[0].field == "invoice_date"


def test_ledger_upload_generates_versioned_debtor_creditor_schedules_and_flags(client, login, two_firms, db):
    firm = two_firms["a"]
    partner = models.User(
        firm_id=firm["firm"].id, name="Ledger Partner", email="ledger-partner@alpha-ca.in",
        role=models.UserRole.PARTNER, password_hash=hash_password("test-password-123"), email_verified=True,
    )
    db.add(partner)
    db.flush()
    engagement = models.AuditEngagement(
        firm_id=firm["firm"].id, client_id=firm["client"].id, financial_year="2025-26",
        engagement_type=models.AuditEngagementType.STATUTORY,
        engagement_partner_id=partner.id, created_by=firm["users"]["manager"].id,
    )
    db.add(engagement)
    db.commit()

    ledger = (
        b"account_type,account_code,account_name,party_name,invoice_number,invoice_date,due_date,open_balance,explanation\n"
        b"DEBTORS,AR,Trade Debtors,Acme Ltd,INV-OLD,2025-01-01,2025-09-01,1500.00,Payment plan noted\n"
        b"DEBTORS,AR,Trade Debtors,New Co,INV-NEW,2026-03-20,,250.00,\n"
        b"CREDITORS,AP,Trade Creditors,Paper Co,BILL-31,2026-01-01,2026-02-28,900.00,\n"
    )
    response = client.post(
        f"/api/audit-engagements/{engagement.id}/ledger?as_of_date=2026-03-31",
        headers=login("associate@alpha-ca.in"),
        files={"file": ("ledger.csv", ledger, "text/csv")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["upload"]["version"] == 1
    assert body["ageing_flags_generated"] == 1
    debtor = next(item for item in body["schedules"] if item["schedule_type"] == "DEBTORS")
    creditor = next(item for item in body["schedules"] if item["schedule_type"] == "CREDITORS")
    assert debtor["data"]["bucket_totals"]["180+"] == "1500.00"
    assert debtor["data"]["oldest_bucket_reviews"][0]["explanation_on_file"] is True
    assert creditor["data"]["bucket_totals"]["31-60"] == "900.00"

    flags = client.get(f"/api/audit-engagements/{engagement.id}/risk-flags", headers=login("associate@alpha-ca.in"))
    assert flags.status_code == 200
    assert flags.json()[0]["status"] == "SYSTEM_FLAGGED"
    assert "aged but explained" in flags.json()[0]["system_note"]

    history = client.get(f"/api/audit-engagements/{engagement.id}/ageing-schedules", headers=login("associate@alpha-ca.in"))
    assert history.status_code == 200
    assert len(history.json()) == 2
