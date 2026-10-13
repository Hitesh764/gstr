from app import models


def _add_partner(db, firm_id: str, slug: str):
    partner = models.User(
        firm_id=firm_id,
        name=f"Partner {slug}",
        email=f"partner@{slug}-ca.in",
        role=models.UserRole.PARTNER,
        password_hash="test-hash",
        email_verified=True,
    )
    db.add(partner)
    db.commit()
    return partner


def test_manager_creates_engagement_and_duplicate_is_rejected(
    client, login, two_firms, db,
):
    firm_a = two_firms["a"]
    partner = _add_partner(db, firm_a["firm"].id, "alpha")
    headers = login("manager@alpha-ca.in")
    payload = {
        "client_id": firm_a["client"].id,
        "financial_year": "2025-26",
        "engagement_type": "STATUTORY",
        "engagement_partner_id": partner.id,
    }

    response = client.post("/api/audit-engagements", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "PLANNING"
    assert created["created_by"] == firm_a["users"]["manager"].id
    assert created["engagement_partner_id"] == partner.id
    assert created["materiality_amount"] is None
    assert created["caro_applicable"] is None

    duplicate = client.post("/api/audit-engagements", json=payload, headers=headers)
    assert duplicate.status_code == 409

    listed = client.get("/api/audit-engagements?financial_year=2025-26", headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [created["id"]]


def test_engagement_endpoints_do_not_cross_firm_boundaries(
    client, login, two_firms, db,
):
    firm_a, firm_b = two_firms["a"], two_firms["b"]
    partner_b = _add_partner(db, firm_b["firm"].id, "bravo")
    foreign = models.AuditEngagement(
        firm_id=firm_b["firm"].id,
        client_id=firm_b["client"].id,
        financial_year="2025-26",
        engagement_type=models.AuditEngagementType.STATUTORY,
        engagement_partner_id=partner_b.id,
        created_by=firm_b["users"]["manager"].id,
    )
    db.add(foreign)
    db.commit()

    headers_a = login("manager@alpha-ca.in")
    assert client.get(f"/api/audit-engagements/{foreign.id}", headers=headers_a).status_code == 404
    assert client.get("/api/audit-engagements", headers=headers_a).json() == []

    partner_a = _add_partner(db, firm_a["firm"].id, "alpha")
    create_foreign_client = client.post(
        "/api/audit-engagements",
        json={
            "client_id": firm_b["client"].id,
            "financial_year": "2025-26",
            "engagement_type": "TAX",
            "engagement_partner_id": partner_a.id,
        },
        headers=headers_a,
    )
    assert create_foreign_client.status_code == 404


def test_only_manager_or_above_can_create_engagements(client, login, two_firms, db):
    firm_a = two_firms["a"]
    partner = _add_partner(db, firm_a["firm"].id, "alpha")
    response = client.post(
        "/api/audit-engagements",
        json={
            "client_id": firm_a["client"].id,
            "financial_year": "2025-26",
            "engagement_type": "INTERNAL",
            "engagement_partner_id": partner.id,
        },
        headers=login("associate@alpha-ca.in"),
    )
    assert response.status_code == 403


def test_financial_year_must_be_consecutive(client, login, two_firms, db):
    firm_a = two_firms["a"]
    partner = _add_partner(db, firm_a["firm"].id, "alpha")
    response = client.post(
        "/api/audit-engagements",
        json={
            "client_id": firm_a["client"].id,
            "financial_year": "2025-27",
            "engagement_type": "STATUTORY",
            "engagement_partner_id": partner.id,
        },
        headers=login("manager@alpha-ca.in"),
    )
    assert response.status_code == 422
