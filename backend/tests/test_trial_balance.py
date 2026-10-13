from decimal import Decimal
from datetime import date

from app import models
from app.ingestion.trial_balance import parse_trial_balance_file
from app.security import hash_password
from app.services.trial_balance import calculate_variance
from app.services.audit_assessments import variance_flag_candidate


def _engagement(db, firm):
    partner = models.User(
        firm_id=firm["firm"].id,
        name="Audit Partner",
        email=f"audit-partner-{firm['firm'].id}@example.test",
        role=models.UserRole.PARTNER,
        password_hash=hash_password("test-password-123"),
        email_verified=True,
    )
    db.add(partner)
    db.flush()
    engagement = models.AuditEngagement(
        firm_id=firm["firm"].id,
        client_id=firm["client"].id,
        financial_year="2025-26",
        engagement_type=models.AuditEngagementType.STATUTORY,
        engagement_partner_id=partner.id,
        created_by=firm["users"]["manager"].id,
    )
    db.add(engagement)
    db.commit()
    return engagement


def test_variance_calculation_is_decimal_precise_and_zero_base_is_undefined():
    amount, pct = calculate_variance(Decimal("69"), Decimal("42"))
    assert amount == Decimal("27.00")
    assert pct == Decimal("64.2857")
    assert calculate_variance(Decimal("18"), Decimal("0")) == (Decimal("18.00"), None)
    assert calculate_variance(Decimal("0"), Decimal("0")) == (
        Decimal("0.00"), Decimal("0.0000"),
    )


def test_variance_flag_uses_only_configured_percentage_and_confirmed_materiality():
    class Line:
        variance_amount = Decimal("500.00")
        variance_pct = Decimal("10.0000")
        current_year_amount = Decimal("5500.00")
        prior_year_amount = Decimal("5000.00")

    triggered, details = variance_flag_candidate(
        Line(), percentage_threshold=Decimal("15"), materiality_amount=Decimal("400"),
        materiality_multiplier=Decimal("1"),
    )
    assert triggered is True
    assert details["percentage_triggered"] is False
    assert details["absolute_triggered"] is True
    assert details["absolute_threshold"] == "400"


def test_trial_balance_parser_reports_row_and_field_errors():
    # Use a plain CSV fixture with a malformed amount and ensure no silent repair.
    contents = (
        "account_code,account_name,account_group,amount\n"
        "100,Repairs,Expenses,100.00\n"
        "200,Cash,Assets,₹1000\n"
    ).encode("utf-8")
    result = parse_trial_balance_file(contents, "tb.csv")
    assert len(result.rows) == 1
    assert [(error.row, error.field) for error in result.errors] == [(3, "amount")]


def test_compare_upload_pairs_snapshots_persists_variance_and_versions(client, login, two_firms, db):
    firm_a = two_firms["a"]
    engagement = _engagement(db, firm_a)
    headers = login("associate@alpha-ca.in")
    current = (
        b"account_code,account_name,account_group,amount\n"
        b"100,Repairs & Maintenance,Expense,690000.00\n"
        b"200,Cash,Current Assets,50000.00\n"
        b"300,New Account,Expense,18000.00\n"
    )
    prior = (
        b"account_code,account_name,account_group,amount\n"
        b"100,Repairs & Maintenance,Expense,420000.00\n"
        b"200,Cash,Current Assets,50000.00\n"
        b"400,Old Account,Expense,3200.00\n"
    )
    files = {
        "current_file": ("current.csv", current, "text/csv"),
        "prior_file": ("prior.csv", prior, "text/csv"),
    }
    response = client.post(
        f"/api/audit-engagements/{engagement.id}/trial-balances/compare",
        files=files,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True
    assert body["inserted"] == 4
    assert body["new_accounts"] == ["300"]
    assert body["prior_only_accounts"] == ["400"]
    assert body["current_upload"]["compares_to_upload_id"] == body["prior_upload"]["id"]

    lines = client.get(
        f"/api/audit-engagements/{engagement.id}/trial-balances/"
        f"{body['current_upload']['id']}/lines",
        headers=headers,
    )
    assert lines.status_code == 200
    by_code = {row["account_code"]: row for row in lines.json()}
    assert by_code["100"]["variance_amount"] == "270000.00"
    assert by_code["100"]["variance_pct"] == "64.2857"
    assert by_code["300"]["prior_year_amount"] == "0.00"
    assert by_code["300"]["variance_pct"] is None
    assert by_code["400"]["current_year_amount"] == "0.00"
    assert by_code["400"]["variance_amount"] == "-3200.00"

    source = client.get(
        f"/api/audit-engagements/{engagement.id}/trial-balances/"
        f"{body['current_upload']['id']}/file",
        headers=headers,
    )
    assert source.status_code == 200
    assert source.content == current

    second = client.post(
        f"/api/audit-engagements/{engagement.id}/trial-balances/compare",
        files=files,
        headers=headers,
    )
    assert second.status_code == 200
    history = client.get(
        f"/api/audit-engagements/{engagement.id}/trial-balances?period_type=CLOSING",
        headers=headers,
    ).json()
    assert [row["version"] for row in history] == [2, 1]
    assert [row["is_current"] for row in history] == [True, False]


def test_comparison_generates_persisted_system_flag_under_firm_thresholds(client, login, two_firms, db):
    firm = two_firms["a"]
    engagement = _engagement(db, firm)
    partner = db.query(models.User).filter_by(email=f"audit-partner-{firm['firm'].id}@example.test").one()
    policy = models.FirmAuditPolicy(
        firm_id=firm["firm"].id, version=1, status=models.AuditPolicyStatus.APPROVED,
        effective_from=date(2025, 4, 1), materiality_default_basis=models.MaterialityBasis.TURNOVER,
        materiality_percentages={"TURNOVER": "2"}, variance_threshold_pct=Decimal("15"),
        variance_materiality_multiplier=Decimal("1"), ratio_movement_threshold_pct=Decimal("20"),
        created_by=firm["users"]["manager"].id, approved_by=partner.id,
    )
    db.add(policy)
    engagement.materiality_amount = Decimal("100000")
    engagement.materiality_confirmed_by = partner.id
    db.commit()
    headers = login("associate@alpha-ca.in")
    response = client.post(
        f"/api/audit-engagements/{engagement.id}/trial-balances/compare",
        files={
            "current_file": ("current.csv", b"account_code,account_name,account_group,amount\n100,Repairs,Expense,690.00\n", "text/csv"),
            "prior_file": ("prior.csv", b"account_code,account_name,account_group,amount\n100,Repairs,Expense,420.00\n", "text/csv"),
        }, headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["risk_flags_generated"] == 1
    flags = client.get(f"/api/audit-engagements/{engagement.id}/risk-flags", headers=headers)
    assert flags.status_code == 200
    assert flags.json()[0]["status"] == "SYSTEM_FLAGGED"
    assert flags.json()[0]["flag_type"] == "VARIANCE"
    assert "no cause" in flags.json()[0]["system_note"]


def test_comparison_validation_does_not_replace_current_version(client, login, two_firms, db):
    engagement = _engagement(db, two_firms["a"])
    headers = login("associate@alpha-ca.in")
    valid = b"account_code,account_name,amount\n100,Cash,100.00\n"
    invalid = b"account_code,account_name,amount\n100,Cash,100.00\n100,Cash duplicate,200.00\n"

    response = client.post(
        f"/api/audit-engagements/{engagement.id}/trial-balances/compare",
        files={
            "current_file": ("current.csv", valid, "text/csv"),
            "prior_file": ("prior.csv", invalid, "text/csv"),
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["errors"][0]["row"] == 3
    assert client.get(
        f"/api/audit-engagements/{engagement.id}/trial-balances", headers=headers,
    ).json() == []


def test_opening_upload_is_versioned_and_cross_firm_engagement_is_hidden(
    client, login, two_firms, db,
):
    engagement = _engagement(db, two_firms["a"])
    foreign = _engagement(db, two_firms["b"])
    headers = login("associate@alpha-ca.in")
    response = client.post(
        f"/api/audit-engagements/{engagement.id}/trial-balances/opening",
        files={"file": ("opening.csv", b"account_code,account_name,amount\n100,Cash,500.00\n", "text/csv")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True
    hidden = client.get(
        f"/api/audit-engagements/{foreign.id}/trial-balances", headers=headers,
    )
    assert hidden.status_code == 404
