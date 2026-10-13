"""Shared builders for the notice tests: real PDFs, a scripted analyser, sample text."""
from datetime import date
from decimal import Decimal
from typing import Callable, Optional

from app.notice_ai import AnalysisError, DraftResult, EvidencePack, NoticeAnalyzer, NoticeFacts


def make_pdf(lines: list[str]) -> bytes:
    """A minimal but genuine PDF with a text layer, built by hand.

    Real bytes through the real reader beat mocking the reader: the extraction
    tests then exercise pypdf itself.
    """
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    stream_lines = ["BT", "/F1 11 Tf", "50 780 Td", "14 TL"]
    for line in lines:
        stream_lines.append(f"({esc(line)}) Tj T*")
    stream_lines.append("ET")
    stream = "\n".join(stream_lines).encode("latin-1", "replace")
    return _assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ])


def make_blank_pdf() -> bytes:
    """A page with no text at all, which is what a scan looks like to a text reader."""
    return _assemble([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>",
    ])


def _assemble(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64      # passes the signature check only

NOTICE_LINES = [
    "OFFICE OF THE ASSISTANT COMMISSIONER, CGST",
    "Notice No: ASMT10/2026/0042",
    "Date: 12/05/2026",
    "Notice for discrepancies in returns under Section 61 of the CGST Act",
    "GSTIN: 27AAPFU0939F1ZV     Tax period: March 2026",
    "1. Explain the difference between ITC claimed and ITC available in GSTR-2B.",
    "2. Furnish reconciliation of purchases from supplier 29AAGCB7383J1Z4.",
    "Amount of ITC in question: Rs. 1,80,000",
    "Please submit your reply on or before 30/05/2026.",
]


def notice_pdf() -> bytes:
    return make_pdf(NOTICE_LINES)


class FakeAnalyzer(NoticeAnalyzer):
    """Scripted stand-in: returns canned facts and builds a draft from the
    evidence it is given, or misbehaves on demand."""
    provider = "fake"
    model = "fake-1"

    def __init__(self, facts: Optional[NoticeFacts] = None):
        self.facts = facts or NoticeFacts(
            notice_type="SCRUTINY_ASMT10", title="ASMT-10 for March 2026",
            reference="ASMT10/2026/0042", section="Section 61",
            notice_date=date(2026, 5, 12), response_deadline=date(2026, 5, 30),
            deadline_basis="on or before 30/05/2026", period_from="2026-03",
            period_to="2026-03", amount=Decimal("180000"), gstin="27AAPFU0939F1ZV",
            questions=["Explain the ITC difference.", "Furnish reconciliation."],
            summary="Scrutiny notice.", confidence="high",
        )
        self.analyse_calls: list[str] = []
        self.draft_calls: list[dict] = []
        self.analyse_error: Optional[AnalysisError] = None
        self.draft_error: Optional[AnalysisError] = None
        self.draft_fn: Optional[Callable[[EvidencePack], DraftResult]] = None

    def analyse(self, notice_text: str) -> NoticeFacts:
        self.analyse_calls.append(notice_text)
        if self.analyse_error:
            raise self.analyse_error
        return self.facts

    def draft_response(self, *, facts, notice_text, evidence) -> DraftResult:
        self.draft_calls.append({"facts": facts, "text": notice_text, "evidence": evidence})
        if self.draft_error:
            raise self.draft_error
        if self.draft_fn:
            return self.draft_fn(evidence)
        ids = [i.id for i in evidence.items]
        body = "Reply to the notice.\n" + "\n".join(f"Record [{i}]" for i in ids)
        return DraftResult(text=body, cited_evidence_ids=ids)
