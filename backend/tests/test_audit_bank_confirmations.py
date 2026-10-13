from app import crypto, models
from app.security import hash_password


def test_bank_confirmation_requires_approval_and_records_reconciled_discrepancy(client, login, two_firms, db):
    firm = two_firms["a"]
    manager = login("manager@alpha-ca.in")
    associate = login("associate@alpha-ca.in")
    account = client.post(f"/api/clients/{firm['client'].id}/bank-accounts", headers=manager, json={
        "bank_name": "Example Bank", "branch_name": "Fort Branch",
        "account_name": "Alpha Client Pvt Ltd", "account_number": "123456789012",
        "ifsc_code": "ABCD0123456",
    })
    assert account.status_code == 201, account.text
    account_body = account.json()
    assert account_body["masked_account_number"] == "•••• 9012"
    stored = db.query(models.ClientBankAccount).filter_by(id=account_body["id"]).one()
    assert stored.account_number_encrypted != "123456789012"
    assert crypto.decrypt(stored.account_number_encrypted) == "123456789012"

    partner = models.User(
        firm_id=firm["firm"].id, name="Bank Partner", email="bank-partner@alpha-ca.in",
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

    generated = client.post(
        f"/api/audit-engagements/{engagement.id}/bank-confirmations/generate",
        headers=associate, json={"as_of_date": "2026-03-31"},
    )
    assert generated.status_code == 200, generated.text
    letter = generated.json()[0]
    assert letter["letter_status"] == "DRAFTED"
    assert "[FIRM LETTERHEAD PLACEHOLDER]" in letter["draft_content"]
    assert "has not been sent" in letter["draft_content"]
    assert "123456789012" not in letter["draft_content"]

    path = f"/api/audit-engagements/{engagement.id}/bank-confirmations/{letter['id']}"
    assert client.post(path + "/send", headers=manager).status_code == 409
    approved = client.post(path + "/approve", headers=manager)
    assert approved.status_code == 200, approved.text
    sent = client.post(path + "/send", headers=manager)
    assert sent.status_code == 200, sent.text
    assert sent.json()["letter_status"] == "SENT"
    assert client.post(path + "/pending", headers=associate).json()["letter_status"] == "PENDING"

    received = client.post(path + "/receive", headers=associate, json={
        "books_balance": "10000.00", "confirmed_balance": "9800.00",
    })
    assert received.status_code == 200, received.text
    assert received.json()["discrepancy_amount"] == "-200.00"
    flags = client.get(f"/api/audit-engagements/{engagement.id}/risk-flags", headers=associate)
    assert any(flag["trigger_data"].get("subtype") == "BANK_CONFIRMATION_DISCREPANCY" for flag in flags.json())

    assert client.post(path + "/reconcile", headers=manager, json={}).status_code == 422
    reconciled = client.post(path + "/reconcile", headers=manager, json={
        "notes": "Difference agrees to outstanding bank charges; follow-up evidence retained.",
    })
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["letter_status"] == "RECONCILED"


def test_client_bank_accounts_are_tenant_scoped(client, login, two_firms):
    firms = two_firms
    response = client.post(f"/api/clients/{firms['b']['client'].id}/bank-accounts",
                           headers=login("manager@alpha-ca.in"), json={
        "bank_name": "Example Bank", "account_number": "123456789012",
    })
    assert response.status_code == 404
