"""
Notice analysis: the interface, and the shapes that cross it.

`NoticeAnalyzer` is the seam between the notice workflow and whatever reads
notices. Two implementations exist: a rule-based mock for local development
and a Claude-backed one. Everything else (storage, workflow, approvals,
alerts) depends only on this interface and on the dataclasses below, so
changing the model, or the provider, touches one file.

Two calls, deliberately separate:

* `analyse(text)` reads the notice and extracts its facts. It sees the notice
  text and nothing else.
* `draft_response(...)` writes a first-pass reply. It runs later, after a
  person has reviewed the extracted facts, and it is handed the client's
  reconciliation records as numbered evidence. It is instructed to cite only
  that evidence, and `services.notices.check_grounding` verifies the citations
  afterwards rather than trusting the instruction was followed.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


class AnalysisError(Exception):
    """The analyser could not produce a usable result.

    `retryable` separates "try again shortly" (rate limit, timeout, provider
    outage) from "trying again will not help" (bad key, refusal, no credentials
    configured), so the caller can show the right advice.
    """

    def __init__(self, message: str, *, code: str = "ANALYSIS_FAILED", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# Notice categories the product recognises. The codes are stored in the
# database; the labels are for people. "OTHER" is always the safe fallback.
NOTICE_TYPES: dict[str, str] = {
    "SCRUTINY_ASMT10": "Scrutiny of returns (ASMT-10, Section 61)",
    "ITC_MISMATCH": "ITC mismatch / excess claim (DRC-01B, GSTR-2B vs 3B)",
    "DEMAND_SCN_73": "Show cause notice, non-fraud demand (Section 73)",
    "DEMAND_SCN_74": "Show cause notice, fraud or suppression (Section 74)",
    "REGISTRATION_SCN": "Show cause for cancellation of registration (REG-17)",
    "RETURN_DEFAULT": "Non-filing of returns (GSTR-3A)",
    "AUDIT_65": "Audit by tax authorities (Section 65)",
    "REFUND_SCN": "Refund deficiency or rejection notice (RFD-08)",
    "OTHER": "Other / could not classify",
}


@dataclass
class NoticeFacts:
    """What was read from the notice. Every field may be absent: a notice that
    does not state a deadline must produce `None`, never a guess."""
    notice_type: str = "OTHER"
    title: Optional[str] = None
    reference: Optional[str] = None
    section: Optional[str] = None
    notice_date: Optional[date] = None
    response_deadline: Optional[date] = None
    # The wording the deadline came from, or the relative rule ("within 30 days
    # of receipt") when no calendar date is stated. Shown next to the deadline
    # so a person can check it in seconds.
    deadline_basis: Optional[str] = None
    period_from: Optional[str] = None      # YYYY-MM
    period_to: Optional[str] = None
    amount: Optional[Decimal] = None
    gstin: Optional[str] = None
    issuing_authority: Optional[str] = None
    questions: list[str] = field(default_factory=list)
    summary: str = ""
    confidence: str = "medium"             # "high" | "medium" | "low"
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "notice_type": self.notice_type,
            "title": self.title,
            "reference": self.reference,
            "section": self.section,
            "notice_date": self.notice_date.isoformat() if self.notice_date else None,
            "response_deadline": self.response_deadline.isoformat() if self.response_deadline else None,
            "deadline_basis": self.deadline_basis,
            "period_from": self.period_from,
            "period_to": self.period_to,
            "amount": str(self.amount) if self.amount is not None else None,
            "gstin": self.gstin,
            "issuing_authority": self.issuing_authority,
            "questions": list(self.questions),
            "summary": self.summary,
            "confidence": self.confidence,
            "warnings": list(self.warnings),
        }


@dataclass
class EvidenceItem:
    """One real record from the client's reconciliation data.

    `id` is what a drafted response cites, as `[E3]`. `text` is the single
    plain-language line handed to the drafter; `data` carries the same facts in
    structured form for the screen and for the grounding check.
    """
    id: str
    kind: str                      # "RUN_SUMMARY" | "EXCEPTION"
    text: str
    data: dict = field(default_factory=dict)


@dataclass
class EvidencePack:
    gstin: Optional[str] = None
    periods: list[str] = field(default_factory=list)
    items: list[EvidenceItem] = field(default_factory=list)
    # Reasons the pack is thinner than it might be: no GSTIN linked, no run for
    # a month, list truncated. Passed to the drafter so it can say so instead
    # of papering over the hole, and shown to the person reviewing.
    gaps: list[str] = field(default_factory=list)

    @property
    def ids(self) -> set[str]:
        return {i.id for i in self.items}

    @property
    def has_data(self) -> bool:
        return bool(self.items)


@dataclass
class DraftResult:
    text: str
    cited_evidence_ids: list[str] = field(default_factory=list)
    # Things the drafter could not supply from the evidence and left for the
    # practitioner, in plain words.
    open_points: list[str] = field(default_factory=list)


class NoticeAnalyzer(ABC):
    provider: str = "unknown"
    model: str = "unknown"

    @abstractmethod
    def analyse(self, notice_text: str) -> NoticeFacts: ...

    @abstractmethod
    def draft_response(
        self, *, facts: NoticeFacts, notice_text: str, evidence: EvidencePack,
    ) -> DraftResult: ...
