"""
The notice workflow over HTTP: intake, reading, drafting, the approval chain.

Extraction runs for real (pypdf reading genuine PDFs). Only the AI analyser is
scripted, because it is the one piece that needs a network and an API key.
"""
import os
from datetime import date, timedelta

import pytest

from app import audit, models
from app.config import settings
from app.main import app
from app.notice_ai import AnalysisError, DraftResult
from app.notifications import NotificationType
from app.routers import notices as notices_router
from app.security import hash_password
from tests.conftest import TEST_PASSWORD
from tests.notice_helpers import (
    NOTICE_LINES, FakeAnalyzer, make_blank_pdf, make_pdf, notice_pdf,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class Ctx:
    def __init__(self, client, login, db, world, ai):
        self.client, self.db, self.w, self.ai = client, db, world, ai
        self.h = {
            "associate": login("associate@alpha-ca.in"),
            "manager": login("manager@alpha-ca.in"),
            "owner": login("owner@alpha-ca.in"),
        }
        partner = models.User(
            firm_id=world["firm"].id, name="Partner Alpha", email="partner@alpha-ca.in",
            role=models.UserRole.PARTNER, password_hash=hash_password(TEST_PASSWORD),
            email_verified=True)
        db.add(partner)
        db.commit()
        self.partner = partner
        self.h["partner"] = login("partner@alpha-ca.in")
        self.users = {**world["users"], "partner": partner}

    def upload(self, who="associate", pdf=None, filename="notice.pdf", **form):
        data = {"client_id": self.w["client"].id, **form}
        return self.client.post(
            "/api/notices", data=data, headers=self.h[who],
            files={"file": (filename, pdf if pdf is not None else notice_pdf(), "application/pdf")})

    def go(self, notice_id, to, who="associate", **body):
        return self.client.post(f"/api/notices/{notice_id}/transition",
                                json={"to_status": to, **body}, headers=self.h[who])

    def get(self, notice_id, who="associate"):
        return self.client.get(f"/api/notices/{notice_id}", headers=self.h[who]).json()

    def patch(self, notice_id, who="associate", **body):
        return self.client.patch(f"/api/notices/{notice_id}", json=body, headers=self.h[who])

    def to_review(self, notice_id, who="associate"):
        r = self.go(notice_id, "UNDER_REVIEW", who)
        assert r.status_code == 200, r.text
        return r.json()

    def to_drafted(self, notice_id, drafter="associate", submitter=None):
        """Under review, draft written by `drafter`, deadline confirmed, submitted."""
        self.to_review(notice_id, drafter)
        r = self.client.put(f"/api/notices/{notice_id}/draft", headers=self.h[drafter],
                            json={"text": "Our reply to the notice."})
        assert r.status_code == 200, r.text
        assert self.patch(notice_id, drafter, confirm_deadline=True).status_code == 200
        r = self.go(notice_id, "RESPONSE_DRAFTED", submitter or drafter)
        assert r.status_code == 200, r.text
        return r.json()

    def actions(self, notice_id, who):
        return {a["to"]: a for a in self.get(notice_id, who)["actions"]}

    def notifications(self, user_key, type_=None):
        self.db.expire_all()
        q = self.db.query(models.Notification).filter(
            models.Notification.user_id == self.users[user_key].id)
        if type_:
            q = q.filter(models.Notification.type == type_)
        return q.all()


@pytest.fixture
def ctx(client, login, db, two_firms):
    ai = FakeAnalyzer()
    app.dependency_overrides[notices_router.get_analyzer] = lambda: ai
    app.dependency_overrides[notices_router.get_optional_analyzer] = lambda: ai
    return Ctx(client, login, db, two_firms["a"], ai)


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

def test_upload_stores_reads_and_analyses(ctx):
    r = ctx.upload()
    assert r.status_code == 201, r.text
    n = r.json()
    assert n["status"] == "RECEIVED"
    assert n["extraction_status"] == "DONE" and n["extraction_method"] == "PDF_TEXT"
    assert "Section 61" in n["ocr_text"]
    assert n["analysis_status"] == "DONE"
    assert n["notice_type"] == "SCRUTINY_ASMT10" and n["section"] == "Section 61"
    assert n["notice_reference"] == "ASMT10/2026/0042"
    assert n["response_deadline"] == "2026-05-30"
    assert n["amount_in_question"] == "180000.00"
    assert n["period"] == "2026-03"
    assert ctx.ai.analyse_calls and "Section 61" in ctx.ai.analyse_calls[0]


def test_the_only_gstin_of_a_client_is_linked_automatically(ctx):
    n = ctx.upload().json()
    assert n["client_gstin_id"] == ctx.w["gstin"].id
    assert n["gstin"] == ctx.w["gstin"].gstin


def test_a_deadline_read_by_the_analyser_is_unconfirmed(ctx):
    n = ctx.upload().json()
    assert n["response_deadline"] == "2026-05-30" and n["deadline_confirmed"] is False


def test_the_file_is_encrypted_on_disk_and_downloads_intact(ctx):
    n = ctx.upload().json()
    files = [os.path.join(d, f) for d, _, fs in os.walk(settings.storage_local_dir) for f in fs]
    assert len(files) == 1
    raw = open(files[0], "rb").read()
    assert b"Section 61" not in raw and b"%PDF" not in raw

    r = ctx.client.get(f"/api/notices/{n['id']}/file", headers=ctx.h["associate"])
    assert r.status_code == 200 and r.content == notice_pdf()
    assert r.headers["content-type"] == "application/pdf"
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    history = ctx.client.get(f"/api/notices/{n['id']}/history", headers=ctx.h["associate"]).json()
    assert "NOTICE_FILE_DOWNLOADED" in [e["action"] for e in history]


def test_the_real_type_of_the_file_is_checked_not_its_name(ctx):
    r = ctx.upload(pdf=b"MZ\x90\x00 pretending to be a pdf", filename="notice.pdf")
    assert r.status_code == 415


def test_empty_and_oversize_files_are_refused(ctx, monkeypatch):
    assert ctx.upload(pdf=b"").status_code == 422
    monkeypatch.setattr(settings, "max_notice_upload_mb", 1)
    big = notice_pdf() + b"0" * (1024 * 1024)
    assert ctx.upload(pdf=big).status_code == 413
    assert ctx.db.query(models.Notice).count() == 0


def test_a_scan_is_kept_flagged_and_not_sent_for_analysis(ctx):
    n = ctx.upload(pdf=make_blank_pdf()).json()
    assert n["extraction_status"] == "NEEDS_OCR"
    assert "scan" in n["extraction_message"].lower()
    assert n["analysis_status"] == "NOT_RUN" and ctx.ai.analyse_calls == []
    assert n["file_available"] is True


def test_typing_in_the_text_rescues_a_scan(ctx):
    n = ctx.upload(pdf=make_blank_pdf()).json()
    r = ctx.client.put(f"/api/notices/{n['id']}/text", headers=ctx.h["associate"],
                       json={"text": "\n".join(NOTICE_LINES)})
    assert r.status_code == 200 and r.json()["extraction_status"] == "MANUAL"
    r = ctx.client.post(f"/api/notices/{n['id']}/analyse", headers=ctx.h["associate"])
    assert r.status_code == 200 and r.json()["analysis_status"] == "DONE"
    assert r.json()["response_deadline"] == "2026-05-30"


def test_analysing_a_notice_with_no_text_is_a_clear_409(ctx):
    n = ctx.upload(pdf=make_blank_pdf()).json()
    r = ctx.client.post(f"/api/notices/{n['id']}/analyse", headers=ctx.h["associate"])
    assert r.status_code == 409 and "no text" in r.json()["detail"]


def test_an_analyser_outage_never_loses_the_upload(ctx):
    ctx.ai.analyse_error = AnalysisError("The AI service is down.", code="UNAVAILABLE", retryable=True)
    r = ctx.upload()
    assert r.status_code == 201
    n = r.json()
    assert n["analysis_status"] == "FAILED" and "down" in n["analysis_error"]
    assert n["ocr_text"] and n["file_available"]
    assert n["notice_type"] is None                       # nothing half-filled

    ctx.ai.analyse_error = None
    again = ctx.client.post(f"/api/notices/{n['id']}/analyse", headers=ctx.h["associate"])
    assert again.json()["analysis_status"] == "DONE" and again.json()["analysis_error"] is None


def test_an_unconfigured_analyser_also_keeps_the_upload(ctx):
    app.dependency_overrides[notices_router.get_optional_analyzer] = lambda: None
    n = ctx.upload().json()
    assert n["analysis_status"] == "FAILED" and "not configured" in n["analysis_error"]


def test_reanalysis_never_overwrites_a_persons_corrections(ctx):
    n = ctx.upload().json()
    ctx.patch(n["id"], title="Our own title", response_deadline="2026-06-15")
    ctx.ai.facts.title = "Analyser title"
    ctx.ai.facts.response_deadline = date(2026, 5, 1)

    kept = ctx.client.post(f"/api/notices/{n['id']}/analyse", headers=ctx.h["associate"]).json()
    assert kept["title"] == "Our own title"
    assert kept["response_deadline"] == "2026-06-15" and kept["deadline_confirmed"] is True

    forced = ctx.client.post(f"/api/notices/{n['id']}/analyse", json={"overwrite": True},
                             headers=ctx.h["associate"]).json()
    assert forced["title"] == "Analyser title"
    assert forced["response_deadline"] == "2026-05-01" and forced["deadline_confirmed"] is False


def test_the_original_reading_survives_a_correction(ctx):
    n = ctx.upload().json()
    ctx.patch(n["id"], notice_reference="CORRECTED/1")
    got = ctx.get(n["id"])
    assert got["notice_reference"] == "CORRECTED/1"
    assert got["ai_analysis"]["facts"]["reference"] == "ASMT10/2026/0042"
    assert got["ai_analysis"]["provider"] == "fake"


def test_a_gstin_mismatch_between_notice_and_record_is_surfaced(ctx):
    ctx.ai.facts.gstin = "29AAGCB7383J1Z4"
    n = ctx.upload().json()
    warnings = n["ai_analysis"]["facts"]["warnings"]
    assert any("29AAGCB7383J1Z4" in w and "linked to" in w for w in warnings)


# ---------------------------------------------------------------------------
# Editing facts
# ---------------------------------------------------------------------------

def test_edits_are_validated(ctx):
    nid = ctx.upload().json()["id"]
    assert ctx.patch(nid, notice_type="MADE_UP").status_code == 422
    assert ctx.patch(nid, period="2026-13").status_code == 422
    assert ctx.patch(nid, period="2026-06", period_to="2026-03").status_code == 422
    assert ctx.patch(nid, amount_in_question="-5").status_code == 422
    assert ctx.patch(nid).status_code == 422          # nothing to change


def test_setting_a_deadline_by_hand_confirms_it(ctx):
    nid = ctx.upload().json()["id"]
    r = ctx.patch(nid, response_deadline="2026-06-20").json()
    assert r["response_deadline"] == "2026-06-20" and r["deadline_confirmed"] is True


def test_confirming_without_a_deadline_is_refused(ctx):
    ctx.ai.facts.response_deadline = None
    nid = ctx.upload().json()["id"]
    assert ctx.patch(nid, confirm_deadline=True).status_code == 422


def test_edits_are_audited_with_old_and_new_values(ctx):
    nid = ctx.upload().json()["id"]
    ctx.patch(nid, section="Section 73")
    history = ctx.client.get(f"/api/notices/{nid}/history", headers=ctx.h["associate"]).json()
    edit = [e for e in history if e["action"] == "NOTICE_UPDATED"][-1]
    assert edit["details"]["changes"]["section"] == {"from": "Section 61", "to": "Section 73"}
    assert edit["user_name"] == ctx.users["associate"].name


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def test_the_full_pipeline_from_receipt_to_closure(ctx):
    nid = ctx.upload().json()["id"]

    under = ctx.to_review(nid)
    assert under["status"] == "UNDER_REVIEW"
    assert under["assigned_to"] == ctx.users["associate"].id          # picked up by whoever starts

    assert ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["associate"],
                          json={"text": "Our reply."}).status_code == 200
    ctx.patch(nid, confirm_deadline=True)
    drafted = ctx.go(nid, "RESPONSE_DRAFTED").json()
    assert drafted["status"] == "RESPONSE_DRAFTED"
    assert drafted["submitted_by"] == ctx.users["associate"].id

    approved = ctx.go(nid, "PARTNER_APPROVED", "partner").json()
    assert approved["status"] == "PARTNER_APPROVED"
    assert approved["approved_by_name"] == "Partner Alpha"

    filed = ctx.go(nid, "FILED", "manager", filing_reference="ARN-778", filed_at=date.today().isoformat()).json()
    assert filed["status"] == "FILED" and filed["filing_reference"] == "ARN-778"

    closed = ctx.go(nid, "CLOSED", "manager", outcome="Proceedings dropped by the department.").json()
    assert closed["status"] == "CLOSED" and closed["outcome"].startswith("Proceedings dropped")
    assert closed["actions"] == []

    history = ctx.client.get(f"/api/notices/{nid}/history", headers=ctx.h["owner"]).json()
    moves = [(e["details"]["from"], e["details"]["to"]) for e in history
             if e["action"] == "NOTICE_STATUS_CHANGED"]
    assert moves == [("RECEIVED", "UNDER_REVIEW"), ("UNDER_REVIEW", "RESPONSE_DRAFTED"),
                     ("RESPONSE_DRAFTED", "PARTNER_APPROVED"), ("PARTNER_APPROVED", "FILED"),
                     ("FILED", "CLOSED")]
    chain = audit.verify_chain(ctx.db, ctx.w["firm"].id)
    assert chain["valid"] is True, chain


def test_submitting_tells_the_partners_but_not_the_submitter(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    assert ctx.notifications("partner", NotificationType.NOTICE_NEEDS_APPROVAL)
    assert ctx.notifications("owner", NotificationType.NOTICE_NEEDS_APPROVAL)
    assert not ctx.notifications("associate", NotificationType.NOTICE_NEEDS_APPROVAL)
    assert not ctx.notifications("manager", NotificationType.NOTICE_NEEDS_APPROVAL)   # managers cannot approve


def test_approval_tells_the_associate(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    ctx.go(nid, "PARTNER_APPROVED", "partner")
    assert ctx.notifications("associate", NotificationType.NOTICE_APPROVED)


def test_the_drafter_cannot_approve_even_as_the_owner(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid, drafter="owner")
    r = ctx.go(nid, "PARTNER_APPROVED", "owner")
    assert r.status_code == 403 and "someone else must approve" in r.json()["detail"]
    assert ctx.go(nid, "PARTNER_APPROVED", "partner").status_code == 200   # a second partner can


def test_the_submitter_cannot_approve_what_someone_else_wrote(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid, drafter="associate", submitter="owner")
    assert ctx.go(nid, "PARTNER_APPROVED", "owner").status_code == 403
    assert ctx.go(nid, "PARTNER_APPROVED", "partner").status_code == 200


def test_whoever_last_edited_the_text_cannot_approve_it(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_review(nid)
    ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["associate"], json={"text": "first"})
    ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["partner"], json={"text": "partner rewrote it"})
    ctx.patch(nid, confirm_deadline=True)
    assert ctx.go(nid, "RESPONSE_DRAFTED", "associate").status_code == 200
    assert ctx.go(nid, "PARTNER_APPROVED", "partner").status_code == 403
    assert ctx.go(nid, "PARTNER_APPROVED", "owner").status_code == 200


def test_associates_and_managers_cannot_approve(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    assert ctx.go(nid, "PARTNER_APPROVED", "manager").status_code == 403
    assert ctx.go(nid, "PARTNER_APPROVED", "associate").status_code == 403


def test_filing_and_closing_need_a_manager(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    ctx.go(nid, "PARTNER_APPROVED", "partner")
    assert ctx.go(nid, "FILED", "associate").status_code == 403
    assert ctx.go(nid, "FILED", "manager").status_code == 200
    assert ctx.go(nid, "CLOSED", "associate", outcome="x").status_code == 403


def test_a_client_viewer_cannot_touch_notices_at_all(ctx):
    viewer = models.User(
        firm_id=ctx.w["firm"].id, name="Viewer", email="viewer@alpha-ca.in",
        role=models.UserRole.CLIENT_VIEWER, scoped_client_id=ctx.w["client"].id,
        password_hash=hash_password(TEST_PASSWORD), email_verified=True)
    ctx.db.add(viewer)
    ctx.db.commit()
    nid = ctx.upload().json()["id"]
    tok = ctx.client.post("/api/auth/login", json={"email": "viewer@alpha-ca.in",
                                                   "password": TEST_PASSWORD}).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert ctx.client.get("/api/notices", headers=h).status_code == 403
    assert ctx.client.get(f"/api/notices/{nid}", headers=h).status_code == 403
    assert ctx.client.get(f"/api/notices/{nid}/file", headers=h).status_code == 403


def test_a_response_cannot_be_submitted_without_a_confirmed_deadline(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_review(nid)
    ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["associate"], json={"text": "reply"})
    r = ctx.go(nid, "RESPONSE_DRAFTED")
    assert r.status_code == 422 and "Confirm the response deadline" in r.json()["detail"]
    ctx.patch(nid, confirm_deadline=True)
    assert ctx.go(nid, "RESPONSE_DRAFTED").status_code == 200


def test_a_response_cannot_be_submitted_without_a_deadline_or_a_draft(ctx):
    ctx.ai.facts.response_deadline = None
    nid = ctx.upload().json()["id"]
    ctx.to_review(nid)
    assert "Write or generate a response" in ctx.go(nid, "RESPONSE_DRAFTED").json()["detail"]
    ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["associate"], json={"text": "reply"})
    assert "Set the response deadline" in ctx.go(nid, "RESPONSE_DRAFTED").json()["detail"]


def test_stages_cannot_be_skipped_or_reversed(ctx):
    nid = ctx.upload().json()["id"]
    assert ctx.go(nid, "FILED", "manager").status_code == 409
    assert ctx.go(nid, "PARTNER_APPROVED", "partner").status_code == 409
    assert ctx.go(nid, "CLOSED", "manager", outcome="x").status_code == 409
    ctx.to_review(nid)
    assert ctx.go(nid, "RECEIVED").status_code == 409        # not a valid target at all
    assert ctx.go(nid, "PARTNER_APPROVED", "partner").status_code == 409


def test_closing_needs_an_outcome_and_filing_cannot_be_dated_in_the_future(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    ctx.go(nid, "PARTNER_APPROVED", "partner")
    future = (date.today() + timedelta(days=5)).isoformat()
    assert ctx.go(nid, "FILED", "manager", filed_at=future).status_code == 422
    ctx.go(nid, "FILED", "manager")
    assert ctx.go(nid, "CLOSED", "manager").status_code == 422
    assert ctx.go(nid, "CLOSED", "manager", outcome="  ").status_code == 422
    assert ctx.go(nid, "CLOSED", "manager", outcome="Dropped").status_code == 200


def test_a_partner_sends_it_back_with_a_reason_and_the_drafter_is_told(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    assert ctx.go(nid, "UNDER_REVIEW", "partner").status_code == 422        # reason required
    r = ctx.go(nid, "UNDER_REVIEW", "partner", reason="Cite the GSTR-2B for July.")
    assert r.status_code == 200
    n = r.json()
    assert n["status"] == "UNDER_REVIEW" and n["rework_reason"] == "Cite the GSTR-2B for July."
    assert n["submitted_by"] is None and n["approved_by"] is None
    assert ctx.notifications("associate", NotificationType.NOTICE_SENT_BACK)

    # editable again, resubmittable, and the reason clears
    assert ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["associate"],
                          json={"text": "revised"}).status_code == 200
    assert ctx.go(nid, "RESPONSE_DRAFTED").json()["rework_reason"] is None


def test_the_submitter_can_withdraw_without_a_reason_but_a_stranger_cannot(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    assert ctx.go(nid, "UNDER_REVIEW", "manager").status_code == 403       # neither author nor partner
    assert ctx.go(nid, "UNDER_REVIEW", "associate").status_code == 200


def test_content_is_locked_while_awaiting_approval(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    put = ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["associate"], json={"text": "sneaky edit"})
    assert put.status_code == 409
    assert ctx.patch(nid, section="Section 74").status_code == 409
    assert ctx.client.put(f"/api/notices/{nid}/text", headers=ctx.h["associate"],
                          json={"text": "x" * 30}).status_code == 409
    assert ctx.client.post(f"/api/notices/{nid}/analyse", headers=ctx.h["associate"]).status_code == 409
    assert ctx.get(nid)["drafted_response"] == "Our reply to the notice."


def test_a_deadline_extension_can_still_be_recorded_before_filing(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    assert ctx.patch(nid, response_deadline="2026-07-31").status_code == 200
    ctx.go(nid, "PARTNER_APPROVED", "partner")
    ctx.go(nid, "FILED", "manager")
    assert ctx.patch(nid, response_deadline="2026-08-31").status_code == 409


def test_the_actions_list_says_in_advance_what_each_person_may_do(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_drafted(nid)
    assoc = ctx.actions(nid, "associate")
    assert assoc["PARTNER_APPROVED"]["allowed"] is False
    assert "someone else" in assoc["PARTNER_APPROVED"]["blocked_reason"] or \
        "Partner" in assoc["PARTNER_APPROVED"]["blocked_reason"]
    assert assoc["UNDER_REVIEW"]["allowed"] is True                        # may withdraw
    assert ctx.actions(nid, "partner")["PARTNER_APPROVED"]["allowed"] is True
    assert ctx.actions(nid, "manager")["UNDER_REVIEW"]["allowed"] is False


def test_assignment_notifies_the_assignee_and_checks_who_they_are(ctx):
    nid = ctx.upload().json()["id"]
    r = ctx.patch(nid, "manager", assigned_to=ctx.users["associate"].id)
    assert r.status_code == 200 and r.json()["assigned_to_name"] == ctx.users["associate"].name
    assert ctx.notifications("associate", NotificationType.NOTICE_ASSIGNED)
    assert ctx.patch(nid, "manager", assigned_to="no-such-user").status_code == 404


def test_deleting_a_notice_needs_a_manager_and_hides_it(ctx):
    nid = ctx.upload().json()["id"]
    assert ctx.client.delete(f"/api/notices/{nid}", headers=ctx.h["associate"]).status_code == 403
    assert ctx.client.delete(f"/api/notices/{nid}", headers=ctx.h["manager"]).status_code == 204
    assert ctx.client.get(f"/api/notices/{nid}", headers=ctx.h["manager"]).status_code == 404
    assert ctx.client.get("/api/notices", headers=ctx.h["manager"]).json() == []
    row = ctx.db.query(models.Notice).filter(models.Notice.id == nid).one()
    assert row.deleted_at is not None and row.uploaded_file_path        # retained, not destroyed


# ---------------------------------------------------------------------------
# Drafting from real records
# ---------------------------------------------------------------------------

def test_evidence_is_the_clients_own_reconciliation_and_nobody_elses(ctx, two_firms):
    nid = ctx.upload().json()["id"]
    ev = ctx.client.get(f"/api/notices/{nid}/evidence", headers=ctx.h["associate"]).json()
    text = " ".join(i["text"] for i in ev["items"])
    assert ev["gstin"] == ctx.w["gstin"].gstin and ev["periods"] == ["2026-03"]
    assert "INV-alpha-001" in text and "Vendor alpha" in text
    assert "Rs 1,800.00" in text                                  # tax = 900 + 900, Indian format
    assert "Secret note belonging to Alpha & Co" in text
    assert "Bravo" not in text and "bravo" not in text
    assert [i["id"] for i in ev["items"]] == ["E1", "E2"]         # summary, then the exception
    assert ev["items"][0]["kind"] == "RUN_SUMMARY"


def test_the_draft_is_built_from_that_evidence_and_records_where_it_came_from(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_review(nid)
    r = ctx.client.post(f"/api/notices/{nid}/draft/generate", headers=ctx.h["associate"])
    assert r.status_code == 200, r.text
    n = r.json()
    assert "[E1]" in n["drafted_response"] and "[E2]" in n["drafted_response"]
    assert n["drafted_by"] == ctx.users["associate"].id
    meta = n["ai_analysis"]["draft"]
    assert meta["cited_evidence_ids"] == ["E1", "E2"] and meta["provider"] == "fake"
    assert ctx.ai.draft_calls[0]["evidence"].gstin == ctx.w["gstin"].gstin


def test_a_draft_citing_records_that_do_not_exist_is_discarded(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_review(nid)
    ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["associate"], json={"text": "my own words"})
    ctx.ai.draft_fn = lambda ev: DraftResult(text="Per [E99] the client owes nothing.",
                                              cited_evidence_ids=["E99"])
    r = ctx.client.post(f"/api/notices/{nid}/draft/generate", json={"replace": True},
                        headers=ctx.h["associate"])
    assert r.status_code == 502 and "E99" in r.json()["detail"]
    assert ctx.get(nid)["drafted_response"] == "my own words"       # untouched


def test_an_invented_amount_is_flagged_for_the_reviewer(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_review(nid)
    ctx.ai.draft_fn = lambda ev: DraftResult(text="The tax is Rs 55,555.00 [E2].", cited_evidence_ids=["E2"])
    n = ctx.client.post(f"/api/notices/{nid}/draft/generate", headers=ctx.h["associate"]).json()
    assert n["ai_analysis"]["draft"]["unverified_amounts"] == ["55,555.00"]


def test_generating_over_an_existing_draft_needs_confirmation(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_review(nid)
    assert ctx.client.post(f"/api/notices/{nid}/draft/generate", headers=ctx.h["associate"]).status_code == 200
    again = ctx.client.post(f"/api/notices/{nid}/draft/generate", headers=ctx.h["associate"])
    assert again.status_code == 409 and "replace" in again.json()["detail"]
    ok = ctx.client.post(f"/api/notices/{nid}/draft/generate", json={"replace": True}, headers=ctx.h["associate"])
    assert ok.status_code == 200


def test_drafting_needs_the_review_to_have_started(ctx):
    nid = ctx.upload().json()["id"]
    assert ctx.client.post(f"/api/notices/{nid}/draft/generate", headers=ctx.h["associate"]).status_code == 409
    assert ctx.client.put(f"/api/notices/{nid}/draft", headers=ctx.h["associate"],
                          json={"text": "x"}).status_code == 409


def test_a_drafter_failure_is_reported_and_leaves_no_draft(ctx):
    nid = ctx.upload().json()["id"]
    ctx.to_review(nid)
    ctx.ai.draft_error = AnalysisError("rate limited", code="RATE_LIMITED", retryable=True)
    r = ctx.client.post(f"/api/notices/{nid}/draft/generate", headers=ctx.h["associate"])
    assert r.status_code == 502 and "rate limited" in r.json()["detail"]
    ctx.ai.draft_error = AnalysisError("bad key", code="AUTH", retryable=False)
    assert ctx.client.post(f"/api/notices/{nid}/draft/generate",
                           headers=ctx.h["associate"]).status_code == 422
    assert ctx.get(nid)["drafted_response"] is None


def test_with_no_gstin_or_period_the_evidence_says_why_it_is_empty(ctx):
    ctx.ai.facts.period_from = ctx.ai.facts.period_to = None
    nid = ctx.upload().json()["id"]
    ev = ctx.client.get(f"/api/notices/{nid}/evidence", headers=ctx.h["associate"]).json()
    assert ev["items"] == [] and any("no tax period" in g for g in ev["gaps"])


def test_a_period_with_no_reconciliation_is_reported_as_a_gap(ctx):
    nid = ctx.upload().json()["id"]
    ctx.patch(nid, period="2026-01", period_to="2026-03")
    ev = ctx.client.get(f"/api/notices/{nid}/evidence", headers=ctx.h["associate"]).json()
    assert ev["periods"] == ["2026-01", "2026-02", "2026-03"]
    assert {g for g in ev["gaps"]} == {"No completed reconciliation exists for 2026-01.",
                                       "No completed reconciliation exists for 2026-02."}
    assert any("2026-03" in i["text"] for i in ev["items"])


def test_evidence_is_capped_keeping_the_largest_and_says_so(ctx, monkeypatch):
    from decimal import Decimal
    monkeypatch.setattr(settings, "notice_evidence_max_items", 3)
    run = ctx.w["run"]
    for n in range(4):
        entry = models.PurchaseRegisterEntry(
            firm_id=ctx.w["firm"].id, upload_id=ctx.w["upload"].id, vendor_name=f"V{n}",
            vendor_gstin="29AAGCB7383J1Z4", invoice_number=f"BIG-{n}", invoice_date=date(2026, 3, 11),
            taxable_value=Decimal("1000"), cgst=Decimal(str(100 * (n + 1) * 100)), sgst=Decimal("0"),
            igst=Decimal("0"), total_value=Decimal("0"))
        ctx.db.add(entry)
        ctx.db.flush()
        res = models.ReconciliationResult(
            firm_id=ctx.w["firm"].id, run_id=run.id, purchase_entry_id=entry.id,
            match_status=models.MatchStatus.SUPPLIER_NOT_FILED, confidence_score=0, match_key=f"big-{n}")
        ctx.db.add(res)
        ctx.db.flush()
        ctx.db.add(models.ReconciliationException(
            firm_id=ctx.w["firm"].id, reconciliation_result_id=res.id,
            client_gstin_id=ctx.w["gstin"].id, period="2026-03", match_key=f"big-{n}",
            status=models.ExceptionStatus.OPEN))
    ctx.db.commit()
    nid = ctx.upload().json()["id"]
    ev = ctx.client.get(f"/api/notices/{nid}/evidence", headers=ctx.h["associate"]).json()
    texts = [i["text"] for i in ev["items"]]
    assert len(ev["items"]) == 3
    assert any("BIG-3" in t for t in texts) and any("BIG-2" in t for t in texts)   # largest kept
    assert not any("BIG-0" in t for t in texts)
    assert any("Only the 2 flagged invoices" in g for g in ev["gaps"])


# ---------------------------------------------------------------------------
# The firm-wide view and client history
# ---------------------------------------------------------------------------

def _make(ctx, deadline, **form):
    ctx.ai.facts.response_deadline = deadline
    return ctx.upload(**form).json()["id"]


def test_open_notices_are_sorted_by_nearest_deadline_with_undated_last(ctx):
    late = _make(ctx, date.today() + timedelta(days=40))
    soon = _make(ctx, date.today() + timedelta(days=2))
    undated = _make(ctx, None)
    overdue = _make(ctx, date.today() - timedelta(days=3))
    rows = ctx.client.get("/api/notices?open_only=true", headers=ctx.h["associate"]).json()
    assert [r["id"] for r in rows] == [overdue, soon, late, undated]
    assert [r["urgency"] for r in rows] == ["overdue", "critical", "ok", "none"]
    assert rows[0]["days_until_deadline"] == -3 and rows[1]["days_until_deadline"] == 2


def test_open_only_hides_filed_notices_but_client_history_keeps_them(ctx):
    a = _make(ctx, date.today() + timedelta(days=5))
    b = _make(ctx, date.today() + timedelta(days=9))
    ctx.to_drafted(a)
    ctx.go(a, "PARTNER_APPROVED", "partner")
    ctx.go(a, "FILED", "manager")
    open_ids = [r["id"] for r in ctx.client.get("/api/notices?open_only=true", headers=ctx.h["associate"]).json()]
    assert open_ids == [b]
    history = ctx.client.get(f"/api/notices?client_id={ctx.w['client'].id}", headers=ctx.h["associate"]).json()
    assert {r["id"] for r in history} == {a, b}
    assert next(r for r in history if r["id"] == a)["urgency"] == "none"     # filed: no urgency


def test_filters_and_the_me_shortcut(ctx):
    a = _make(ctx, date.today() + timedelta(days=5))
    b = _make(ctx, date.today() + timedelta(days=9))
    ctx.patch(a, "manager", assigned_to=ctx.users["associate"].id)
    mine = ctx.client.get("/api/notices?assigned_to=me", headers=ctx.h["associate"]).json()
    assert [r["id"] for r in mine] == [a]
    ctx.to_review(b, "manager")
    by_status = ctx.client.get("/api/notices?status=UNDER_REVIEW", headers=ctx.h["associate"]).json()
    assert [r["id"] for r in by_status] == [b]


def test_stats_summarise_the_workload(ctx):
    _make(ctx, date.today() - timedelta(days=1))
    _make(ctx, date.today() + timedelta(days=4))
    _make(ctx, date.today() + timedelta(days=60))
    _make(ctx, None)
    drafted = _make(ctx, date.today() + timedelta(days=10))
    ctx.to_drafted(drafted)
    s = ctx.client.get("/api/notices/stats", headers=ctx.h["associate"]).json()
    assert s == {"open": 5, "overdue": 1, "due_within_7_days": 1, "awaiting_approval": 1,
                 "unassigned": 4, "no_deadline": 1}


def test_notice_type_vocabulary_is_served(ctx):
    types = ctx.client.get("/api/notices/types", headers=ctx.h["associate"]).json()
    assert {"code": "OTHER", "label": "Other / could not classify"} in types
