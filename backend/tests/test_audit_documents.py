from datetime import date
from types import SimpleNamespace
from io import BytesIO
from zipfile import ZipFile

from app import models
from app.security import hash_password
from app.services.audit_analysis import AuditAnalysisService


def test_audit_analysis_accepts_safe_draft_and_regenerates_rejected_response():
    class FakeMessages:
        def __init__(self):
            self.responses = ["The clause is complied with.", "SYSTEM DRAFT â€” Records show two filing entries for CA verification."]
            self.prompts = []

        def create(self, **kwargs):
            self.prompts.append(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(text=self.responses.pop(0))])

    messages = FakeMessages()
    service = AuditAnalysisService(provider="claude", model="test-model", api_key="key",
                                   client=SimpleNamespace(messages=messages))
    result = service.draft_caro(clause_number=7, title="Statutory dues", clause_text="context",
                                source_snapshot={"filings": 2})
    assert result.narrative.startswith("SYSTEM DRAFT")
    assert len(result.attempts) == 2
    assert result.attempts[0].accepted is False
    assert result.attempts[1].accepted is True
    assert result.attempts[0].exact_prompt != result.attempts[1].exact_prompt
    assert all("Never make, imply, or answer a CARO conclusion" in item["system"] for item in messages.prompts)


def _document_engagement(db, firm, partner, associate):
    rule = models.CAROApplicabilityRule(version=1, effective_from=date(2025, 4, 1),
        rule_data={"all_clause_numbers": []}, source_reference="unit test CARO rule")
    db.add(rule); db.flush()
    engagement = models.AuditEngagement(
        firm_id=firm["firm"].id, client_id=firm["client"].id,
        financial_year="2025-26", engagement_type=models.AuditEngagementType.STATUTORY,
        engagement_partner_id=partner.id, created_by=associate.id,
        status=models.AuditEngagementStatus.COMPLETION,
        caro_applicable=False, caro_confirmed_by=partner.id,
    )
    db.add(engagement); db.flush()
    assessment = models.CAROApplicabilityAssessment(firm_id=firm["firm"].id,
        engagement_id=engagement.id, rule_id=rule.id, version=1,
        status=models.AuditAssessmentStatus.CONFIRMED, determined_applicable=False,
        applicable_clause_numbers=[], reasoning="Not applicable for this fixture.", inputs={},
        assessed_by=associate.id, confirmed_applicable=False, confirmed_by=partner.id)
    db.add(assessment); db.flush(); engagement.caro_assessment_id = assessment.id
    db.commit(); db.refresh(engagement)
    return engagement


def test_mrl_requires_manager_and_partner_approval_and_explicit_send(client, login, two_firms, db):
    firm = two_firms["a"]
    partner = models.User(firm_id=firm["firm"].id, name="Assigned Partner",
        email="doc-partner@alpha-ca.in", role=models.UserRole.PARTNER,
        password_hash=hash_password("test-password-123"), email_verified=True)
    db.add(partner); db.flush()
    engagement = _document_engagement(db, firm, partner, firm["users"]["associate"])
    root = f"/api/audit-engagements/{engagement.id}"
    headers = login("associate@alpha-ca.in")
    generated = client.post(f"{root}/management-rep-letters/generate", headers=headers)
    assert generated.status_code == 200, generated.text
    row = generated.json()
    assert row["status"] == "DRAFT"
    assert "SYSTEM DRAFT" in row["draft_content"]
    premature_send = client.post(f"{root}/management-rep-letters/{row['id']}/send", headers=login(partner.email))
    assert premature_send.status_code == 409
    edited = client.put(f"{root}/management-rep-letters/{row['id']}/edit", headers=headers,
                        json={"final_content": "Management reviewed and customized this draft."})
    assert edited.status_code == 200
    manager_review = client.post(f"{root}/management-rep-letters/{row['id']}/review",
        headers=login("manager@alpha-ca.in"), json={"decision": "approve"})
    assert manager_review.status_code == 200
    approved = client.post(f"{root}/management-rep-letters/{row['id']}/approve", headers=login(partner.email))
    assert approved.status_code == 200
    sent = client.post(f"{root}/management-rep-letters/{row['id']}/send", headers=login(partner.email))
    assert sent.status_code == 200
    assert sent.json()["status"] == "SENT"
    assert sent.json()["sent_to_client_at"] is not None


def test_working_paper_export_is_blocked_until_approval_chain(client, login, two_firms, db):
    firm = two_firms["a"]
    partner = models.User(firm_id=firm["firm"].id, name="Export Partner",
        email="export-partner@alpha-ca.in", role=models.UserRole.PARTNER,
        password_hash=hash_password("test-password-123"), email_verified=True)
    db.add(partner); db.flush()
    engagement = _document_engagement(db, firm, partner, firm["users"]["associate"])
    definition = models.CAROClauseDefinition(clause_number=1, title="Property, plant and equipment",
        clause_text="Versioned test reference", applicability_criteria={}, version=1,
        effective_from=date(2025, 4, 1), source_reference="unit test")
    db.add(definition); db.flush()
    db.add(models.CAROClauseResponse(firm_id=firm["firm"].id, engagement_id=engagement.id,
        clause_definition_id=definition.id, clause_number=1, version=1,
        applicability="Not Applicable", applicability_reasoning="Fixture only.",
        draft_narrative="SECRET SYSTEM DRAFT MUST NEVER APPEAR IN REPORT", final_narrative=None,
        source_snapshot={}, status=models.CAROResponseStatus.DRAFT,
        prepared_by=firm["users"]["associate"].id))
    db.commit()
    root = f"/api/audit-engagements/{engagement.id}"
    procedure = client.post(f"{root}/procedures", headers=login("associate@alpha-ca.in"),
        json={"sa_reference": "SA 500", "procedure_name": "Inspect selected ledger support"})
    assert procedure.status_code == 200, procedure.text
    procedure_id = procedure.json()["id"]
    performed = client.post(f"{root}/procedures/{procedure_id}/perform",
        headers=login("associate@alpha-ca.in"),
        json={"conclusion_notes": "Items traced to source records for reviewer consideration.",
              "evidence_file_paths": ["engagement/evidence/sample-1.pdf"]})
    assert performed.status_code == 200, performed.text
    reviewed_procedure = client.post(f"{root}/procedures/{procedure_id}/review",
        headers=login("manager@alpha-ca.in"), json={"decision": "approve"})
    assert reviewed_procedure.status_code == 200
    approved_procedure = client.post(f"{root}/procedures/{procedure_id}/approve", headers=login(partner.email))
    assert approved_procedure.status_code == 200
    unauthorized = client.get(f"{root}/audit-package/export", headers=login(partner.email))
    assert unauthorized.status_code == 409
    premature_close = client.post(f"{root}/close", headers=login(partner.email))
    assert premature_close.status_code == 409
    compiled = client.post(f"{root}/working-papers/compile", headers=login("associate@alpha-ca.in"))
    assert compiled.status_code == 200, compiled.text
    paper = compiled.json()
    draft_export = client.get(f"{root}/audit-package/export", headers=login(partner.email))
    assert draft_export.status_code == 409
    self_review = client.post(f"{root}/working-papers/{paper['id']}/review",
        headers=login("associate@alpha-ca.in"), json={"decision": "approve"})
    assert self_review.status_code == 403
    reviewed = client.post(f"{root}/working-papers/{paper['id']}/review",
        headers=login("manager@alpha-ca.in"), json={"decision": "approve"})
    assert reviewed.status_code == 200
    approved = client.post(f"{root}/working-papers/{paper['id']}/approve", headers=login(partner.email))
    assert approved.status_code == 200
    mrl = client.post(f"{root}/management-rep-letters/generate", headers=login("associate@alpha-ca.in"))
    assert mrl.status_code == 200, mrl.text
    letter_id = mrl.json()["id"]
    assert client.put(f"{root}/management-rep-letters/{letter_id}/edit",
        headers=login("associate@alpha-ca.in"), json={"final_content": "Final management representations, verified and customized."}).status_code == 200
    assert client.post(f"{root}/management-rep-letters/{letter_id}/review",
        headers=login("manager@alpha-ca.in"), json={"decision": "approve"}).status_code == 200
    assert client.post(f"{root}/management-rep-letters/{letter_id}/approve", headers=login(partner.email)).status_code == 200
    exported = client.get(f"{root}/audit-package/export", headers=login(partner.email))
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(exported.content)) as archive:
        package_text = "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist())
    assert "SECRET SYSTEM DRAFT" not in package_text
    reporting = client.post(f"{root}/move-to-reporting", headers=login(partner.email))
    assert reporting.status_code == 200, reporting.text
    closed = client.post(f"{root}/close", headers=login(partner.email))
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "CLOSED"
