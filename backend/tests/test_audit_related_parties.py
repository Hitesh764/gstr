from datetime import date
from decimal import Decimal

from app import models
from app.security import hash_password


def test_exact_related_party_name_and_alias_always_create_deduplicated_review_flags(client, login, two_firms, db):
    firm = two_firms["a"]
    partner = models.User(
        firm_id=firm["firm"].id, name="Related Partner", email="related-partner@alpha-ca.in",
        role=models.UserRole.PARTNER, password_hash=hash_password("test-password-123"), email_verified=True,
    )
    db.add(partner)
    db.flush()
    engagement = models.AuditEngagement(
        firm_id=firm["firm"].id, client_id=firm["client"].id, financial_year="2025-26",
        engagement_type=models.AuditEngagementType.STATUTORY, engagement_partner_id=partner.id,
        created_by=firm["users"]["manager"].id,
    )
    db.add(engagement)
    db.flush()
    upload = models.TrialBalanceUpload(
        firm_id=firm["firm"].id, engagement_id=engagement.id,
        period_type=models.TrialBalancePeriodType.CLOSING, version=1, is_current=True,
        original_filename="closing.csv", file_path="test-key", file_sha256="0" * 64,
        row_count=1, uploaded_by=firm["users"]["associate"].id,
    )
    db.add(upload)
    db.flush()
    db.add(models.TrialBalanceLine(
        firm_id=firm["firm"].id, upload_id=upload.id, account_code="RP-1",
        account_name="Acme Holdings Limited", current_year_amount=Decimal("0.00"),
    ))
    db.commit()

    created = client.post(
        f"/api/clients/{firm['client'].id}/related-parties",
        headers=login("manager@alpha-ca.in"),
        json={"name": "Acme Holdings Ltd.", "aliases": ["Acme Holdings Limited"],
              "relationship_nature": "Entity controlled by director"},
    )
    assert created.status_code == 201, created.text

    flags_response = client.get(
        f"/api/audit-engagements/{engagement.id}/risk-flags",
        headers=login("associate@alpha-ca.in"),
    )
    assert flags_response.status_code == 200, flags_response.text
    flags = flags_response.json()
    assert len(flags) == 1
    flag = flags[0]
    assert flag["flag_type"] == "RELATED_PARTY"
    assert flag["status"] == "SYSTEM_FLAGGED"
    assert flag["source_reference_key"].startswith("RELATED:")
    assert "irrespective of amount" in flag["system_note"]
    assert "not a conclusion" in flag["system_note"]

    rechecked = client.post(
        f"/api/audit-engagements/{engagement.id}/related-party-matches/recheck",
        headers=login("associate@alpha-ca.in"),
    )
    assert rechecked.status_code == 200, rechecked.text
    assert rechecked.json()["matched_sources"] == 1
    assert rechecked.json()["flags_created"] == 0

    deleted = client.delete(
        f"/api/clients/{firm['client'].id}/related-parties/{created.json()['id']}",
        headers=login("manager@alpha-ca.in"),
    )
    assert deleted.status_code == 200, deleted.text
    after_deactivation = client.get(
        f"/api/audit-engagements/{engagement.id}/risk-flags",
        headers=login("associate@alpha-ca.in"),
    )
    assert len(after_deactivation.json()) == 1


def test_party_name_normalization_is_exact_and_does_not_fuzzy_match():
    from app.services.related_party_matching import normalize_party_name

    assert normalize_party_name("  Acme Holdings Ltd. ") == normalize_party_name("ACME-Holdings LTD")
    assert normalize_party_name("Acme Holding") != normalize_party_name("Acme Holdings")
