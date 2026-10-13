from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app import models
from app.security import hash_password
from app.services.audit_ratios import calculate_audit_ratios


def test_ratio_calculation_uses_average_turnover_balances_and_deterministic_thresholds():
    current = SimpleNamespace(
        account_code="ca", current_year_amount=Decimal("1600"), prior_year_amount=Decimal("1200"),
    )
    lines = [
        current,
        SimpleNamespace(account_code="inv", current_year_amount=Decimal("400"), prior_year_amount=Decimal("300")),
        SimpleNamespace(account_code="cl", current_year_amount=Decimal("-800"), prior_year_amount=Decimal("-400")),
        SimpleNamespace(account_code="debt", current_year_amount=Decimal("-300"), prior_year_amount=Decimal("-200")),
        SimpleNamespace(account_code="equity", current_year_amount=Decimal("-1000"), prior_year_amount=Decimal("-800")),
        SimpleNamespace(account_code="rev", current_year_amount=Decimal("-2000"), prior_year_amount=Decimal("-1600")),
        SimpleNamespace(account_code="cogs", current_year_amount=Decimal("1000"), prior_year_amount=Decimal("800")),
        SimpleNamespace(account_code="sales", current_year_amount=Decimal("-1500"), prior_year_amount=Decimal("-1200")),
        SimpleNamespace(account_code="ar", current_year_amount=Decimal("200"), prior_year_amount=Decimal("100")),
        SimpleNamespace(account_code="profit", current_year_amount=Decimal("-200"), prior_year_amount=Decimal("-160")),
    ]
    opening = [
        SimpleNamespace(account_code="inv", current_year_amount=Decimal("200")),
        SimpleNamespace(account_code="ar", current_year_amount=Decimal("100")),
    ]
    mapping = {
        "current_assets": ["ca"], "inventory": ["inv"], "current_liabilities": ["cl"],
        "borrowings": ["debt"], "equity": ["equity"], "revenue": ["rev"],
        "cost_of_goods_sold": ["cogs"], "credit_sales": ["sales"],
        "trade_receivables": ["ar"], "net_profit": ["profit"],
    }
    result = calculate_audit_ratios(
        closing_lines=lines, opening_lines=opening, account_mapping=mapping,
        prior_opening_inventory=Decimal("250"), prior_opening_receivables=Decimal("50"),
        movement_threshold_pct=Decimal("19"),
    )
    assert result["ratios"]["current_ratio"]["current_year"] == "2.0000"
    assert result["ratios"]["current_ratio"]["prior_year"] == "3.0000"
    assert result["ratios"]["current_ratio"]["threshold_exceeded"] is True
    assert result["ratios"]["inventory_turnover"]["current_year"] == "3.3333"
    assert result["ratios"]["gross_profit_margin"]["current_year"] == "0.5000"
    assert "going concern" in result["judgment_boundary"]


def test_turnover_ratio_stays_unavailable_without_opening_balances():
    rows = [SimpleNamespace(account_code=key, current_year_amount=Decimal("100"), prior_year_amount=Decimal("80"))
            for key in ("ca", "inv", "cl", "debt", "equity", "rev", "cogs", "sales", "ar", "profit")]
    mapping = {key: [account] for key, account in {
        "current_assets": "ca", "inventory": "inv", "current_liabilities": "cl", "borrowings": "debt",
        "equity": "equity", "revenue": "rev", "cost_of_goods_sold": "cogs", "credit_sales": "sales",
        "trade_receivables": "ar", "net_profit": "profit",
    }.items()}
    result = calculate_audit_ratios(closing_lines=rows, opening_lines=[], account_mapping=mapping,
                                    movement_threshold_pct=Decimal("20"))
    assert result["ratios"]["inventory_turnover"]["current_year"] is None
    assert result["ratios"]["inventory_turnover"]["unavailable_reason"]


def test_ratio_analysis_requires_manager_review_and_assigned_partner_approval(client, login, two_firms, db):
    firm = two_firms["a"]
    partner = models.User(
        firm_id=firm["firm"].id, name="Ratio Partner", email="ratio-partner@alpha-ca.in",
        role=models.UserRole.PARTNER, password_hash=hash_password("test-password-123"), email_verified=True,
    )
    db.add(partner)
    db.flush()
    engagement = models.AuditEngagement(
        firm_id=firm["firm"].id, client_id=firm["client"].id, financial_year="2025-26",
        engagement_type=models.AuditEngagementType.STATUTORY,
        engagement_partner_id=partner.id, created_by=firm["users"]["manager"].id,
    )
    policy = models.FirmAuditPolicy(
        firm_id=firm["firm"].id, version=1, status=models.AuditPolicyStatus.APPROVED,
        effective_from=date(2025, 4, 1), materiality_default_basis=models.MaterialityBasis.TURNOVER,
        materiality_percentages={"TURNOVER": "2"}, variance_threshold_pct=Decimal("15"),
        variance_materiality_multiplier=Decimal("1"), ratio_movement_threshold_pct=Decimal("10"),
        created_by=firm["users"]["manager"].id, approved_by=partner.id,
    )
    db.add_all([engagement, policy])
    db.flush()
    upload = models.TrialBalanceUpload(
        firm_id=firm["firm"].id, engagement_id=engagement.id,
        period_type=models.TrialBalancePeriodType.CLOSING, version=1, is_current=True,
        original_filename="closing.csv", file_path="test-key", file_sha256="0" * 64,
        row_count=10, uploaded_by=firm["users"]["associate"].id,
    )
    db.add(upload)
    db.flush()
    amounts = {
        "ca": (1600, 1200), "inv": (400, 300), "cl": (-800, -400), "debt": (-300, -200),
        "equity": (-1000, -800), "rev": (-2000, -1600), "cogs": (1000, 800),
        "sales": (-1500, -1200), "ar": (200, 100), "profit": (-200, -160),
    }
    for code, (cy, py) in amounts.items():
        db.add(models.TrialBalanceLine(
            firm_id=firm["firm"].id, upload_id=upload.id, account_code=code,
            account_name=code, current_year_amount=Decimal(cy), prior_year_amount=Decimal(py),
        ))
    opening = models.TrialBalanceUpload(
        firm_id=firm["firm"].id, engagement_id=engagement.id,
        period_type=models.TrialBalancePeriodType.OPENING, version=1, is_current=True,
        original_filename="opening.csv", file_path="opening-key", file_sha256="1" * 64,
        row_count=2, uploaded_by=firm["users"]["associate"].id,
    )
    db.add(opening)
    db.flush()
    for code, amount in (("inv", "200"), ("ar", "100")):
        db.add(models.TrialBalanceLine(
            firm_id=firm["firm"].id, upload_id=opening.id, account_code=code,
            account_name=code, current_year_amount=Decimal(amount),
        ))
    db.commit()

    payload = {
        "current_assets_account_codes": ["ca"], "inventory_account_codes": ["inv"],
        "current_liabilities_account_codes": ["cl"], "borrowings_account_codes": ["debt"],
        "equity_account_codes": ["equity"], "revenue_account_codes": ["rev"],
        "cost_of_goods_sold_account_codes": ["cogs"], "credit_sales_account_codes": ["sales"],
        "trade_receivables_account_codes": ["ar"], "net_profit_account_codes": ["profit"],
        "prior_opening_inventory": "250", "prior_opening_receivables": "50",
        "prior_opening_source_reference": "FY 2024-25 opening TB, imported from audited books",
    }
    associate = login("associate@alpha-ca.in")
    generated = client.post(f"/api/audit-engagements/{engagement.id}/ratio-analyses", headers=associate, json=payload)
    assert generated.status_code == 200, generated.text
    body = generated.json()
    assert body["status"] == "DRAFT"
    assert body["ratio_results"]["ratios"]["current_ratio"]["risk_flag_id"], body["ratio_results"]
    assert client.post(f"/api/audit-engagements/{engagement.id}/ratio-analyses/{body['id']}/approve",
                       headers=login(partner.email)).status_code == 409
    reviewed = client.post(f"/api/audit-engagements/{engagement.id}/ratio-analyses/{body['id']}/review",
                           headers=login("manager@alpha-ca.in"))
    assert reviewed.status_code == 200, reviewed.text
    approved = client.post(f"/api/audit-engagements/{engagement.id}/ratio-analyses/{body['id']}/approve",
                           headers=login(partner.email))
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
