from datetime import date
from types import SimpleNamespace

from app import models
from app.security import hash_password


def _caro_engagement(db, firm, partner, associate):
    rule = models.CAROApplicabilityRule(
        version=1, effective_from=date(2025, 4, 1),
        rule_data={"all_clause_numbers": [7]}, source_reference="fixture CARO rule",
    )
    definition = models.CAROClauseDefinition(
        clause_number=7, title="Statutory dues", clause_text="Versioned clause reference text.",
        applicability_criteria={}, version=1, effective_from=date(2025, 4, 1),
        source_reference="fixture CARO clause",
    )
    db.add_all([rule, definition])
    db.flush()
    engagement = models.AuditEngagement(
        firm_id=firm["firm"].id, client_id=firm["client"].id,
        financial_year="2025-26", engagement_type=models.AuditEngagementType.STATUTORY,
        engagement_partner_id=partner.id, created_by=associate.id,
        status=models.AuditEngagementStatus.FIELDWORK, caro_applicable=True,
        caro_confirmed_by=partner.id,
    )
    db.add(engagement)
    db.flush()
    assessment = models.CAROApplicabilityAssessment(
        firm_id=firm["firm"].id, engagement_id=engagement.id, rule_id=rule.id,
        version=1, status=models.AuditAssessmentStatus.CONFIRMED,
        determined_applicable=True, applicable_clause_numbers=[7],
        reasoning="Partner-confirmed test applicability.", inputs={}, assessed_by=associate.id,
        confirmed_applicable=True, confirmed_by=partner.id,
    )
    db.add(assessment)
    db.flush()
    engagement.caro_assessment_id = assessment.id
    db.commit()
    return engagement


def test_caro_response_requires_manager_then_distinct_assigned_partner_finalization(client, login, two_firms, db):
    firm = two_firms["a"]
    partner = models.User(
        firm_id=firm["firm"].id, name="CARO Partner", email="caro-partner@alpha-ca.in",
        role=models.UserRole.PARTNER, password_hash=hash_password("test-password-123"), email_verified=True,
    )
    db.add(partner)
    db.flush()
    engagement = _caro_engagement(db, firm, partner, firm["users"]["associate"])

    generated = client.post(
        f"/api/audit-engagements/{engagement.id}/caro-responses/generate",
        headers=login("associate@alpha-ca.in"),
    )
    assert generated.status_code == 200, generated.text
    assert len(generated.json()) == 1
    response = generated.json()[0]
    assert response["status"] == "DRAFT"
    assert "SYSTEM DRAFT" in response["draft_narrative"]
    assert response["final_narrative"] is None
    ai_record = db.query(models.AuditAIRecord).filter_by(
        engagement_id=engagement.id, entity_id=response["id"]).one()
    assert ai_record.accepted is True
    assert ai_record.exact_prompt and ai_record.exact_response
    assert len(ai_record.response_sha256) == 64
    ai_log = db.query(models.AuditLog).filter_by(
        entity_type="CAROClauseResponse", entity_id=response["id"],
        action="AUDIT_AI_SUGGESTION_RECORDED").one()
    assert ai_log.meta["exact_prompt"] == ai_record.exact_prompt
    assert ai_log.meta["exact_response"] == ai_record.exact_response

    finalize_url = f"/api/audit-engagements/{engagement.id}/caro-responses/{response['id']}/finalize"
    partner_headers = login(partner.email)
    premature = client.post(finalize_url, headers=partner_headers,
                            json={"final_narrative": "Final after review."})
    assert premature.status_code == 409

    edited = client.put(
        f"/api/audit-engagements/{engagement.id}/caro-responses/{response['id']}/edit",
        headers=login("associate@alpha-ca.in"),
        json={"final_narrative": "Associate draft for manager review."},
    )
    assert edited.status_code == 200, edited.text

    review_url = f"/api/audit-engagements/{engagement.id}/caro-responses/{response['id']}/review"
    self_review = client.post(review_url, headers=login("associate@alpha-ca.in"),
                              json={"decision": "approve"})
    assert self_review.status_code == 403
    reviewed = client.post(review_url, headers=login("manager@alpha-ca.in"),
                           json={"decision": "approve", "notes": "Evidence reviewed."})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "MANAGER_REVIEWED"

    finalized = client.post(finalize_url, headers=partner_headers,
                            json={"final_narrative": "Partner verified final response."})
    assert finalized.status_code == 200, finalized.text
    final_row = finalized.json()
    assert final_row["status"] == "FINALIZED"
    assert final_row["draft_narrative"] != final_row["final_narrative"]
    assert final_row["final_narrative"] == "Partner verified final response."


def test_caro_generation_endpoint_records_rejected_then_accepted_ai_attempt(client, login, two_firms, db, monkeypatch):
    from app.routers import audit_caro_responses
    from app.services.audit_analysis import AuditAnalysisService

    firm = two_firms["a"]
    partner = models.User(firm_id=firm["firm"].id, name="AI Partner",
        email="ai-partner@alpha-ca.in", role=models.UserRole.PARTNER,
        password_hash=hash_password("test-password-123"), email_verified=True)
    db.add(partner); db.flush()
    engagement = _caro_engagement(db, firm, partner, firm["users"]["associate"])

    class FakeMessages:
        def __init__(self):
            self.responses = ["The clause is complied with.", "SYSTEM DRAFT — Records show entries requiring CA verification."]

        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text=self.responses.pop(0))])

    messages = FakeMessages()
    monkeypatch.setattr(audit_caro_responses, "AuditAnalysisService", lambda: AuditAnalysisService(
        provider="claude", model="test-model", api_key="fake-key",
        client=SimpleNamespace(messages=messages)))
    response = client.post(f"/api/audit-engagements/{engagement.id}/caro-responses/generate",
                           headers=login("associate@alpha-ca.in"))
    assert response.status_code == 200, response.text
    draft = response.json()[0]
    assert draft["draft_narrative"].startswith("SYSTEM DRAFT")
    attempts = db.query(models.AuditAIRecord).filter_by(engagement_id=engagement.id,
        entity_id=draft["id"]).order_by(models.AuditAIRecord.attempt_number).all()
    assert [item.accepted for item in attempts] == [False, True]
    ai_logs = db.query(models.AuditLog).filter_by(entity_id=draft["id"],
        action="AUDIT_AI_SUGGESTION_RECORDED").order_by(models.AuditLog.created_at).all()
    assert len(ai_logs) == 2
    assert [item.meta["accepted"] for item in ai_logs] == [False, True]
