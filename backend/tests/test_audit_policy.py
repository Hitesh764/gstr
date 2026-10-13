from datetime import date

from app import models
from app.security import hash_password


def _add_partner(db, firm_id: str, email: str):
    partner = models.User(
        firm_id=firm_id,
        name="Policy Partner",
        email=email,
        role=models.UserRole.PARTNER,
        password_hash=hash_password("test-password-123"),
        email_verified=True,
    )
    db.add(partner)
    db.commit()
    return partner


def _policy_payload(effective_from: str):
    return {
        "effective_from": effective_from,
        "materiality_default_basis": "TURNOVER",
        "materiality_percentages": {
            "TURNOVER": "0.75",
            "PROFIT_BEFORE_TAX": "5.00",
            "NET_WORTH": "1.00",
        },
        "variance_threshold_pct": "15.00",
        "variance_materiality_multiplier": "1.00",
        "ratio_movement_threshold_pct": "20.00",
    }


def test_policy_is_draft_until_different_partner_approves(client, login, two_firms, db):
    firm_a = two_firms["a"]
    partner = _add_partner(db, firm_a["firm"].id, "partner@alpha-ca.in")
    manager_headers = login("manager@alpha-ca.in")

    create = client.post("/api/audit-policy", json=_policy_payload("2025-04-01"),
                         headers=manager_headers)
    assert create.status_code == 201, create.text
    policy = create.json()
    assert policy["version"] == 1
    assert policy["status"] == "DRAFT"
    assert policy["materiality_percentages"]["TURNOVER"] == "0.75"

    assert client.get("/api/audit-policy/active", headers=manager_headers).status_code == 404
    self_approval = client.post(
        f"/api/audit-policy/{policy['id']}/approve",
        headers=manager_headers,
    )
    assert self_approval.status_code == 403

    approved = client.post(
        f"/api/audit-policy/{policy['id']}/approve",
        headers=login(partner.email),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["approved_by"] == partner.id

    active = client.get("/api/audit-policy/active?as_of=2025-06-01", headers=manager_headers)
    assert active.status_code == 200
    assert active.json()["id"] == policy["id"]


def test_policy_self_approval_is_rejected_and_approved_versions_get_nonoverlapping_dates(
    client, login, two_firms, db,
):
    firm_a = two_firms["a"]
    partner_one = _add_partner(db, firm_a["firm"].id, "partner1@alpha-ca.in")
    partner_two = _add_partner(db, firm_a["firm"].id, "partner2@alpha-ca.in")
    headers_one = login(partner_one.email)

    draft = client.post("/api/audit-policy", json=_policy_payload("2025-04-01"),
                        headers=headers_one)
    assert draft.status_code == 201, draft.text
    denied = client.post(
        f"/api/audit-policy/{draft.json()['id']}/approve",
        headers=headers_one,
    )
    assert denied.status_code == 409

    approved = client.post(
        f"/api/audit-policy/{draft.json()['id']}/approve",
        headers=login(partner_two.email),
    )
    assert approved.status_code == 200, approved.text

    next_draft = client.post("/api/audit-policy", json=_policy_payload("2026-04-01"),
                             headers=login("manager@alpha-ca.in"))
    assert next_draft.status_code == 201, next_draft.text
    next_approved = client.post(
        f"/api/audit-policy/{next_draft.json()['id']}/approve",
        headers=headers_one,
    )
    assert next_approved.status_code == 200, next_approved.text

    history = client.get("/api/audit-policy", headers=headers_one).json()
    by_version = {row["version"]: row for row in history}
    assert by_version[1]["effective_to"] == "2026-03-31"
    assert by_version[2]["effective_to"] is None

    overlap = client.post("/api/audit-policy", json=_policy_payload("2025-06-01"),
                          headers=login("manager@alpha-ca.in"))
    assert overlap.status_code == 201
    rejected = client.post(
        f"/api/audit-policy/{overlap.json()['id']}/approve",
        headers=login(partner_two.email),
    )
    assert rejected.status_code == 409


def test_versioned_reference_catalog_is_readable_but_global(client, login, two_firms, db):
    first = models.AuditStandardReference(
        sa_number="SA 315",
        title="Identifying and Assessing Risks",
        summary="Version one reference summary.",
        version=1,
        effective_from=date(2020, 4, 1),
        effective_to=date(2025, 3, 31),
        source_reference="https://www.icai.org/post/auditing-review-and-other-standards-formerly-known-as-aas-complete-text",
    )
    second = models.AuditStandardReference(
        sa_number="SA 315",
        title="Identifying and Assessing Risks",
        summary="Version two reference summary.",
        version=2,
        effective_from=date(2025, 4, 1),
        source_reference="https://www.icai.org/post/auditing-review-and-other-standards-formerly-known-as-aas-complete-text",
    )
    clause = models.CAROClauseDefinition(
        clause_number=7,
        title="Statutory dues",
        clause_text="Versioned clause reference text.",
        applicability_criteria={"source": "CARO order version"},
        version=1,
        effective_from=date(2021, 4, 1),
        source_reference="https://www.mca.gov.in/Ministry/pdf/Orders_25022020.pdf",
    )
    db.add_all([first, second, clause])
    db.commit()

    headers = login("associate@alpha-ca.in")
    current = client.get("/api/audit-references/standards?sa_number=sa%20315&as_of=2026-01-01",
                         headers=headers)
    assert current.status_code == 200
    assert [(r["version"], r["summary"]) for r in current.json()] == [
        (2, "Version two reference summary.")
    ]
    clauses = client.get("/api/audit-references/caro-clauses?clause_number=7&as_of=2026-01-01",
                         headers=headers)
    assert clauses.status_code == 200
    assert clauses.json()[0]["clause_number"] == 7

    # Reference definitions are global and must not be duplicated per firm.
    assert not hasattr(first, "firm_id")
    assert len(two_firms) == 2
