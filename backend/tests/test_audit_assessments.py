from datetime import date, datetime
from decimal import Decimal

from app import models
from app.security import hash_password
from app.services.audit_assessments import assess_caro, calculate_materiality


def test_materiality_uses_configured_decimal_percentage():
    assert calculate_materiality(Decimal("1234567.89"), Decimal("2.5")) == Decimal("30864.20")


def test_caro_rule_data_controls_private_company_thresholds():
    rules = {
        "exempt_boolean_fields": [],
        "private_company_exemption": {
            "private_field": "private", "public_group_field": "public_group",
            "criteria": [{"field": "capital", "operator": "lte", "value": "100", "label": "capital"}],
        },
        "consolidated_only_clause_numbers": [21], "all_clause_numbers": list(range(1, 22)),
    }
    applicable, clauses, _ = assess_caro(rules, {
        "private": True, "public_group": False, "capital": "100",
        "is_consolidated_financial_statements": False,
    })
    assert applicable is False
    assert clauses == []


def _partner(db, firm_id):
    partner = models.User(firm_id=firm_id, name="Engagement Partner", email="engagement-partner@test.in",
                          role=models.UserRole.PARTNER, password_hash=hash_password("test-password-123"), email_verified=True)
    db.add(partner)
    db.commit()
    return partner


def test_planning_requires_partner_confirmations_before_fieldwork(client, login, two_firms, db):
    firm = two_firms["a"]
    partner = _partner(db, firm["firm"].id)
    manager_headers = login("manager@alpha-ca.in")
    partner_headers = login(partner.email)
    associate_headers = login("associate@alpha-ca.in")

    created = client.post("/api/audit-engagements", headers=manager_headers, json={
        "client_id": firm["client"].id, "financial_year": "2025-26",
        "engagement_type": "STATUTORY", "engagement_partner_id": partner.id,
    })
    assert created.status_code == 201, created.text
    engagement_id = created.json()["id"]

    # Load this test firm's approved convention (using the existing approval chain).
    draft = client.post("/api/audit-policy", headers=manager_headers, json={
        "effective_from": "2025-04-01", "materiality_default_basis": "TURNOVER",
        "materiality_percentages": {"TURNOVER": "2", "PROFIT_BEFORE_TAX": "5", "NET_WORTH": "1"},
        "variance_threshold_pct": "15", "variance_materiality_multiplier": "1",
        "ratio_movement_threshold_pct": "20",
    })
    assert draft.status_code == 201, draft.text
    assert client.post(f"/api/audit-policy/{draft.json()['id']}/approve", headers=partner_headers).status_code == 200

    suggestion = client.post(f"/api/audit-engagements/{engagement_id}/materiality/suggest",
        headers=associate_headers, json={"turnover": "1000000", "profit_before_tax": "100000", "net_worth": "500000"})
    assert suggestion.status_code == 200, suggestion.text
    assert suggestion.json()["suggested_amount"] == "20000.00"
    assert client.post(f"/api/audit-engagements/{engagement_id}/start-fieldwork", headers=partner_headers).status_code == 409

    assert client.post(f"/api/audit-engagements/{engagement_id}/materiality/confirm", headers=partner_headers, json={}).status_code == 200
    rule = models.CAROApplicabilityRule(
        id="assessment-test-rule", version=1, effective_from=date(2021, 4, 1), effective_to=None,
        rule_data={"exempt_boolean_fields": [], "private_company_exemption": {
            "private_field": "is_private_company", "public_group_field": "is_holding_or_subsidiary_of_public_company",
            "criteria": [{"field": "paid_up_capital_and_reserves", "operator": "lte", "value": "10000000", "label": "capital"},
                         {"field": "maximum_borrowings_during_year", "operator": "lte", "value": "10000000", "label": "borrowings"},
                         {"field": "total_revenue_including_discontinued_operations", "operator": "lte", "value": "100000000", "label": "revenue"}]},
            "consolidated_only_clause_numbers": [21], "all_clause_numbers": list(range(1, 22))},
        source_reference="test fixture", created_at=datetime.utcnow())
    db.add(rule)
    db.commit()
    assess = client.post(f"/api/audit-engagements/{engagement_id}/caro-applicability/assess", headers=associate_headers, json={
        "paid_up_capital_and_reserves": "20000000", "maximum_borrowings_during_year": "20000000",
        "total_revenue_including_discontinued_operations": "200000000"})
    assert assess.status_code == 200, assess.text
    assert assess.json()["status"] == "PROPOSED"
    assert client.post(f"/api/audit-engagements/{engagement_id}/caro-applicability/confirm", headers=partner_headers,
                       json={"applicable": True}).status_code == 200
    started = client.post(f"/api/audit-engagements/{engagement_id}/start-fieldwork", headers=partner_headers)
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "FIELDWORK"
