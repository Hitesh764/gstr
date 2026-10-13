"""
The pieces under the notice workflow, tested on their own: encrypted storage,
text extraction, the two analysers, the grounding check and the alert schedule.
"""
import os
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx2 as httpx
import pytest

from app.config import settings
from app.notice_ai import (
    NOTICE_TYPES, AnalysisError, DraftResult, EvidenceItem, EvidencePack, MockNoticeAnalyzer,
    NoticeFacts, get_notice_analyzer,
)
from app.notice_ai.claude import ClaudeNoticeAnalyzer, _DraftSchema, _FactsSchema
from app.ocr import (
    ExtractionResult, ExtractionStatus, PdfTextExtractor, TesseractExtractor, TextExtractor,
    extract_text, sniff_content_type,
)
from app.services import notices as svc
from app.storage import (
    LocalEncryptedStorage, StorageConfigurationError, StorageError, get_storage,
)
from tests.notice_helpers import (
    NOTICE_LINES, PNG_BYTES, make_blank_pdf, make_pdf, notice_pdf,
)


# ---------------------------------------------------------------------------
# Encrypted storage
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return LocalEncryptedStorage(str(tmp_path / "vault"))


def test_a_stored_file_round_trips(store):
    key = store.save(firm_id="firmA", category="notices", data=b"hello notice")
    assert store.load(key, firm_id="firmA") == b"hello notice"
    assert store.exists(key, firm_id="firmA")


def test_what_reaches_the_disk_is_not_readable(store, tmp_path):
    secret = b"CONFIDENTIAL-DEMAND-OF-RS-9,99,999"
    key = store.save(firm_id="firmA", category="notices", data=secret)
    on_disk = (tmp_path / "vault" / key).read_bytes()
    assert secret not in on_disk
    assert b"CONFIDENTIAL" not in on_disk


def test_a_key_from_another_firm_is_refused_even_though_the_file_exists(store):
    key = store.save(firm_id="firmA", category="notices", data=b"a's file")
    with pytest.raises(StorageError):
        store.load(key, firm_id="firmB")
    with pytest.raises(StorageError):
        store.delete(key, firm_id="firmB")
    assert store.exists(key, firm_id="firmA")          # untouched by the attempt


@pytest.mark.parametrize("bad_key", [
    "../../etc/passwd", "firmA/notices/../../x.enc", "firmA/notices/x.enc",
    "firmA/notices/" + "a" * 32 + ".txt", "/firmA/notices/" + "a" * 32 + ".enc",
    "", "firmA", "firmA//" + "a" * 32 + ".enc",
])
def test_malformed_or_traversing_keys_are_refused(store, bad_key):
    with pytest.raises(StorageError):
        store.load(bad_key, firm_id="firmA")


def test_unsafe_firm_or_category_cannot_be_used_to_build_a_key(store):
    for firm, category in [("../x", "notices"), ("firmA", "../x"), ("fi/rm", "n")]:
        with pytest.raises(StorageError):
            store.save(firm_id=firm, category=category, data=b"x")


def test_a_file_altered_on_disk_is_detected(store, tmp_path):
    key = store.save(firm_id="firmA", category="notices", data=b"original")
    path = tmp_path / "vault" / key
    blob = bytearray(path.read_bytes())
    blob[-3] ^= 0x01
    path.write_bytes(bytes(blob))
    with pytest.raises(StorageError, match="altered|changed"):
        store.load(key, firm_id="firmA")


def test_delete_is_idempotent(store):
    key = store.save(firm_id="firmA", category="notices", data=b"x")
    store.delete(key, firm_id="firmA")
    store.delete(key, firm_id="firmA")
    assert not store.exists(key, firm_id="firmA")


def test_the_s3_backend_is_honestly_reported_as_not_built(monkeypatch):
    monkeypatch.setattr(settings, "storage_backend", "s3")
    get_storage.cache_clear()
    with pytest.raises(StorageConfigurationError, match="not built"):
        get_storage()


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def test_a_digital_pdf_is_read_from_its_text_layer():
    result = PdfTextExtractor().extract(notice_pdf())
    assert result.status == ExtractionStatus.DONE
    assert result.method == "PDF_TEXT"
    assert "Section 61" in result.text and "30/05/2026" in result.text
    assert result.pages == 1


def test_a_pdf_with_no_text_is_reported_as_a_scan_not_as_empty_success():
    result = PdfTextExtractor().extract(make_blank_pdf())
    assert result.status == ExtractionStatus.NEEDS_OCR
    assert "scan" in result.message.lower()


def test_a_corrupt_pdf_fails_with_a_readable_message():
    result = PdfTextExtractor().extract(b"%PDF-1.4 this is not really a pdf")
    assert result.status == ExtractionStatus.FAILED
    assert result.message


def test_an_image_without_ocr_installed_says_so_instead_of_raising():
    with mock.patch.object(TesseractExtractor, "is_available", return_value=False):
        result = extract_text(PNG_BYTES, "image/png")
    assert result.status == ExtractionStatus.NEEDS_OCR
    assert "not set up" in result.message


def test_an_injected_ocr_reader_is_used_for_images():
    class Fake(TextExtractor):
        def extract(self, data):
            return ExtractionResult(status=ExtractionStatus.DONE, text="x" * 60, method="OCR",
                                    pages=1, warnings=["check the figures"])
    result = extract_text(PNG_BYTES, "image/png", ocr=Fake())
    assert result.status == ExtractionStatus.DONE and result.method == "OCR"


def test_other_types_are_refused():
    assert extract_text(b"x", "text/plain").status == ExtractionStatus.FAILED


def test_file_type_is_decided_by_the_bytes_not_the_name():
    assert sniff_content_type(notice_pdf()) == "application/pdf"
    assert sniff_content_type(PNG_BYTES) == "image/png"
    assert sniff_content_type(b"\xff\xd8\xff\xe0abc") == "image/jpeg"
    assert sniff_content_type(b"MZ\x90\x00 an executable") is None
    assert sniff_content_type(b"<html><script>") is None


# ---------------------------------------------------------------------------
# The rule-based (mock) analyser
# ---------------------------------------------------------------------------

TEXT = "\n".join(NOTICE_LINES)


def test_mock_reads_the_facts_of_a_scrutiny_notice():
    facts = MockNoticeAnalyzer().analyse(TEXT)
    assert facts.notice_type == "SCRUTINY_ASMT10"
    assert facts.section == "Section 61"
    assert facts.reference == "ASMT10/2026/0042"
    assert facts.notice_date == date(2026, 5, 12)
    assert facts.response_deadline == date(2026, 5, 30)
    assert facts.period_from == "2026-03" and facts.period_to == "2026-03"
    assert facts.amount == Decimal("180000")
    assert facts.gstin == "27AAPFU0939F1ZV"
    assert len(facts.questions) >= 2
    assert facts.confidence == "low"          # it never claims more than it can back up


def test_mock_never_computes_a_relative_deadline():
    text = "Notice under Section 73. You are required to reply within 30 days of receipt."
    facts = MockNoticeAnalyzer().analyse(text)
    assert facts.response_deadline is None
    assert "30 days" in (facts.deadline_basis or "")
    assert any("relative deadline" in w for w in facts.warnings)


def test_mock_reports_a_missing_deadline_rather_than_inventing_one():
    facts = MockNoticeAnalyzer().analyse("A letter with no useful content at all.")
    assert facts.response_deadline is None and facts.notice_type == "OTHER"
    assert facts.warnings


@pytest.mark.parametrize("text,expected", [
    ("Financial year 2023-24 notice", ("2023-04", "2024-03")),
    ("period: April 2023 to March 2024", ("2023-04", "2024-03")),
    ("Tax period: July 2025", ("2025-07", "2025-07")),
])
def test_mock_reads_periods(text, expected):
    from app.notice_ai.mock import find_period
    assert find_period(text) == expected


def test_mock_draft_uses_only_supplied_evidence_and_leaves_gaps_open():
    evidence = EvidencePack(items=[
        EvidenceItem(id="E1", kind="RUN_SUMMARY", text="For 2026-03: 10 invoices compared."),
        EvidenceItem(id="E2", kind="EXCEPTION", text="Invoice INV-9 from Acme, tax Rs 900.00."),
    ])
    facts = MockNoticeAnalyzer().analyse(TEXT)
    out = MockNoticeAnalyzer().draft_response(facts=facts, notice_text=TEXT, evidence=evidence)
    assert "[E1]" in out.text and "[E2]" in out.text
    assert set(out.cited_evidence_ids) == {"E1", "E2"}
    assert "TO BE COMPLETED BY PRACTITIONER" in out.text     # the second point has no evidence
    assert out.open_points


def test_mock_draft_without_evidence_is_a_skeleton_not_a_fabrication():
    facts = MockNoticeAnalyzer().analyse(TEXT)
    out = MockNoticeAnalyzer().draft_response(
        facts=facts, notice_text=TEXT, evidence=EvidencePack(gaps=["none linked"]))
    assert out.cited_evidence_ids == []
    assert "no reconciliation data was available" in out.text
    assert "Rs" not in out.text.replace("Rs. ", "")          # no invented amounts


def test_the_mock_is_refused_outside_development(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "notice_ai_provider", "mock")
    get_notice_analyzer.cache_clear()
    with pytest.raises(AnalysisError, match="development"):
        get_notice_analyzer()
    get_notice_analyzer.cache_clear()


def test_claude_provider_without_a_key_says_what_to_set(monkeypatch):
    monkeypatch.setattr(settings, "notice_ai_provider", "claude")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    get_notice_analyzer.cache_clear()
    with pytest.raises(AnalysisError, match="ANTHROPIC_API_KEY") as exc:
        get_notice_analyzer()
    assert exc.value.code == "NOT_CONFIGURED" and not exc.value.retryable
    get_notice_analyzer.cache_clear()


# ---------------------------------------------------------------------------
# The Claude analyser, against a scripted SDK client
# ---------------------------------------------------------------------------

class FakeMessages:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return self.result


def _claude(result=None, raises=None, model="claude-test-model"):
    fake = SimpleNamespace(messages=FakeMessages(result, raises))
    return ClaudeNoticeAnalyzer(api_key="unused", model=model, client=fake), fake.messages


def _reply(parsed, stop="end_turn"):
    return SimpleNamespace(stop_reason=stop, parsed_output=parsed)


def _facts_schema(**over):
    base = dict(notice_type="SCRUTINY_ASMT10", summary="A scrutiny notice.", confidence="high",
                reference="ASMT10/2026/0042", section="Section 61", notice_date="2026-05-12",
                response_deadline="2026-05-30", deadline_basis="on or before 30/05/2026",
                period_from="2026-03", period_to="2026-03", amount="1,80,000",
                gstin="27aapfu0939f1zv", questions=["Explain the ITC difference.", "  "])
    base.update(over)
    return _FactsSchema(**base)


def test_claude_analysis_sends_the_configured_model_and_only_the_notice_text():
    analyzer, calls = _claude(_reply(_facts_schema()), model="claude-configured-model")
    facts = analyzer.analyse("THE NOTICE TEXT")
    call = calls.calls[0]
    assert call["model"] == "claude-configured-model"          # from settings, not hardcoded
    assert call["output_format"] is _FactsSchema               # schema-validated output
    assert "THE NOTICE TEXT" in call["messages"][0]["content"]
    assert "not instructions" in call["system"]                # injection guard present
    assert "Never do date arithmetic" in call["system"]
    assert facts.response_deadline == date(2026, 5, 30)
    assert facts.amount == Decimal("180000")
    assert facts.gstin == "27AAPFU0939F1ZV"                    # normalised
    assert facts.questions == ["Explain the ITC difference."]  # blanks dropped


def test_claude_bad_values_become_warnings_instead_of_crashing():
    analyzer, _ = _claude(_reply(_facts_schema(
        response_deadline="30th of May", amount="a lot", period_from="March", notice_date="2026-13-45")))
    facts = analyzer.analyse("text")
    assert facts.response_deadline is None and facts.amount is None
    assert facts.period_from is None and facts.notice_date is None
    assert sum("was ignored" in w for w in facts.warnings) == 3


def test_claude_draft_sends_the_numbered_evidence_and_the_citation_rules():
    analyzer, calls = _claude(_reply(_DraftSchema(
        draft="Reply [E1]", cited_evidence_ids=["E1", "E1"], open_points=["Explain point 2"])))
    evidence = EvidencePack(items=[EvidenceItem("E1", "EXCEPTION", "Invoice A, tax Rs 5.00")],
                            gaps=["No run for 2026-04"])
    out = analyzer.draft_response(facts=NoticeFacts(reference="R1"), notice_text="N", evidence=evidence)
    sent = calls.calls[0]
    assert "[E1] Invoice A, tax Rs 5.00" in sent["messages"][0]["content"]
    assert "No run for 2026-04" in sent["messages"][0]["content"]
    assert "Do not invent" in sent["system"]
    assert out.cited_evidence_ids == ["E1"]                    # de-duplicated
    assert out.open_points == ["Explain point 2"]


def _status_error(cls, code, message="m"):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx.Response(code, request=req), body=None)


@pytest.mark.parametrize("error,code,retryable", [
    (_status_error(anthropic.AuthenticationError, 401), "AUTH", False),
    (_status_error(anthropic.PermissionDeniedError, 403), "AUTH", False),
    (_status_error(anthropic.NotFoundError, 404), "MODEL_NOT_FOUND", False),
    (_status_error(anthropic.RateLimitError, 429), "RATE_LIMITED", True),
    (_status_error(anthropic.BadRequestError, 400), "REQUEST_REJECTED", False),
    (_status_error(anthropic.InternalServerError, 500), "PROVIDER_ERROR", True),
    (_status_error(anthropic.OverloadedError, 529), "PROVIDER_ERROR", True),
    (anthropic.APIConnectionError(request=httpx.Request("POST", "https://x")), "UNAVAILABLE", True),
    (anthropic.APITimeoutError(request=httpx.Request("POST", "https://x")), "UNAVAILABLE", True),
])
def test_every_api_failure_becomes_a_clear_analysis_error(error, code, retryable):
    analyzer, _ = _claude(raises=error)
    with pytest.raises(AnalysisError) as exc:
        analyzer.analyse("text")
    assert exc.value.code == code
    assert exc.value.retryable is retryable
    assert "sk-" not in str(exc.value)


def test_a_refusal_is_reported_and_not_retried():
    analyzer, _ = _claude(_reply(None, stop="refusal"))
    with pytest.raises(AnalysisError) as exc:
        analyzer.analyse("text")
    assert exc.value.code == "REFUSED" and not exc.value.retryable


def test_a_truncated_reply_is_never_used():
    analyzer, _ = _claude(_reply(_facts_schema(), stop="max_tokens"))
    with pytest.raises(AnalysisError) as exc:
        analyzer.analyse("text")
    assert exc.value.code == "TRUNCATED"


def test_an_unparseable_reply_is_an_error_not_an_empty_result():
    analyzer, _ = _claude(_reply(None))
    with pytest.raises(AnalysisError) as exc:
        analyzer.analyse("text")
    assert exc.value.code == "UNPARSEABLE"


def test_the_schema_offers_every_notice_type_the_product_knows():
    allowed = set(_FactsSchema.model_json_schema()["properties"]["notice_type"]["enum"])
    assert allowed == set(NOTICE_TYPES)


# ---------------------------------------------------------------------------
# The grounding check
# ---------------------------------------------------------------------------

def _pack():
    return EvidencePack(items=[
        EvidenceItem("E1", "RUN_SUMMARY", "For 2026-03: 10 invoices. Unsettled tax Rs 1,800.00."),
        EvidenceItem("E2", "EXCEPTION", "Invoice X, taxable Rs 10,000.00, tax Rs 1,800.00."),
    ])


def test_a_citation_of_a_record_that_does_not_exist_is_caught():
    draft = DraftResult(text="See [E7] for details.", cited_evidence_ids=["E1"])
    report = svc.check_grounding(draft, _pack(), NoticeFacts(), "")
    assert not report.ok and report.unknown_ids == ["E7"]


def test_a_bad_id_in_the_citation_list_alone_is_also_caught():
    draft = DraftResult(text="fine", cited_evidence_ids=["E1", "E99"])
    assert svc.check_grounding(draft, _pack(), NoticeFacts(), "").unknown_ids == ["E99"]


def test_amounts_from_the_evidence_or_the_notice_are_accepted():
    draft = DraftResult(text="Tax of Rs 1,800.00 [E2] against the Rs. 1,80,000 demanded.",
                        cited_evidence_ids=["E2"])
    report = svc.check_grounding(draft, _pack(), NoticeFacts(amount=Decimal("180000")), "")
    assert report.ok and report.unverified_amounts == []


def test_an_amount_that_appears_nowhere_is_flagged_for_the_reviewer():
    draft = DraftResult(text="The client owes Rs 77,777.00 [E1].", cited_evidence_ids=["E1"])
    report = svc.check_grounding(draft, _pack(), NoticeFacts(), "")
    assert report.ok                                    # flagged, not rejected
    assert report.unverified_amounts == ["77,777.00"]


# ---------------------------------------------------------------------------
# Alert schedule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("days_left,expected", [
    (30, None), (8, None), (7, 7), (5, 7), (4, 7), (3, 3), (2, 3), (1, 1),
    (0, 0), (-1, -1), (-40, -1),
])
def test_alert_points(days_left, expected):
    assert svc.alert_point(days_left) == expected


def test_due_today_and_overdue_are_always_alert_points(monkeypatch):
    monkeypatch.setattr(settings, "notice_alert_days", "10")
    assert svc.alert_days() == [-1, 0, 10]


def test_mock_keeps_a_wrapped_numbered_point_whole_and_drops_the_closing_paragraph():
    """A PDF wraps long points over several lines and often loses blank lines,
    so the last point can run into the paragraphs that follow it."""
    text = "\n".join([
        "1. Explain the difference between the credit claimed in GSTR-3B and the",
        "   credit available in your GSTR-2B for the period.",
        "2. Furnish a reconciliation of purchases from suppliers whose invoices do not",
        "   appear in your GSTR-2B.",
        "Amount of input tax credit in question: Rs. 1,80,000",
        "You are requested to submit your reply on or before 26/09/2026.",
    ])
    from app.notice_ai.mock import find_questions
    assert find_questions(text) == [
        "Explain the difference between the credit claimed in GSTR-3B and the credit "
        "available in your GSTR-2B for the period.",
        "Furnish a reconciliation of purchases from suppliers whose invoices do not "
        "appear in your GSTR-2B.",
    ]


def test_mock_quotes_the_line_the_deadline_sits_on():
    text = "Notice under Section 61.\nDate: 01/09/2026\nPlease reply on or before 15/09/2026.\nThanks."
    facts = MockNoticeAnalyzer().analyse(text)
    assert facts.response_deadline == date(2026, 9, 15)
    assert facts.deadline_basis == "Please reply on or before 15/09/2026."
