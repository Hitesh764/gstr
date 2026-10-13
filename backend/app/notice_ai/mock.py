"""
Rule-based stand-in for the Claude analyser, for local development and tests.

It reads a notice with keyword and pattern matching. That is enough to drive
the whole workflow end to end without an API key, and it is honest about its
limits: every result is marked low confidence, so the screen tells the person
to check it. It is refused outside development (see `get_notice_analyzer`),
because a pattern matcher must never be the thing reading a real client's
statutory notice.

The drafted response is a structured template that lays the evidence out
point by point. It invents nothing: every figure it prints comes from the
evidence pack, with the citation attached.
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.notice_ai.base import (
    DraftResult, EvidencePack, NoticeAnalyzer, NoticeFacts,
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_PAT = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")
_WORD_DATE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s,\-]+({_MONTH_PAT})[\s,\-]+(\d{{4}})\b", re.I)
_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
_AMOUNT = re.compile(r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_SECTION = re.compile(r"\bsection\s+(\d{1,3}[A-Z]?(?:\(\d+\))?)", re.I)
_REFERENCE = re.compile(
    r"(?:ref(?:erence)?\.?\s*(?:no\.?|number)?|notice\s*(?:no\.?|number)|din)\s*[:\-]?\s*"
    r"([A-Z0-9][A-Z0-9/\-]{5,40})", re.I)


def _to_date(day: int, month: int, year: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find_dates(text: str) -> list[tuple[int, date]]:
    """Every calendar date in the text, with its position, in reading order."""
    found: list[tuple[int, date]] = []
    for m in _NUMERIC_DATE.finditer(text):
        d = _to_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
    for m in _WORD_DATE.finditer(text):
        d = _to_date(int(m.group(1)), _MONTHS[m.group(2).lower()], int(m.group(3)))
        if d:
            found.append((m.start(), d))
    return sorted(found)


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def find_period(text: str) -> tuple[Optional[str], Optional[str]]:
    """The tax period a notice covers: 'FY 2023-24', 'April 2023 to March 2024',
    or a single month. Returns (from, to) as YYYY-MM, or (None, None)."""
    m = re.search(r"\b(?:F\.?Y\.?|financial\s+year)\s*[:\-]?\s*(\d{4})\s*[-/]\s*(\d{2,4})\b", text, re.I)
    if m:
        start = int(m.group(1))
        return _month_key(start, 4), _month_key(start + 1, 3)
    m = re.search(rf"\b({_MONTH_PAT})[\s,\-]+(\d{{4}})\s+(?:to|-|until)\s+({_MONTH_PAT})[\s,\-]+(\d{{4}})\b",
                  text, re.I)
    if m:
        return (_month_key(int(m.group(2)), _MONTHS[m.group(1).lower()]),
                _month_key(int(m.group(4)), _MONTHS[m.group(3).lower()]))
    m = re.search(rf"\b(?:tax\s+period|period|month\s+of|return\s+period)\s*[:\-]?\s*({_MONTH_PAT})[\s,\-]+(\d{{4}})\b",
                  text, re.I)
    if m:
        key = _month_key(int(m.group(2)), _MONTHS[m.group(1).lower()])
        return key, key
    return None, None


def classify(text: str) -> tuple[str, Optional[str]]:
    """(notice type code, statute section) from keywords. Order matters: the
    more specific signals come first."""
    low = text.lower()
    sec = None
    sm = _SECTION.search(text)
    if sm:
        sec = f"Section {sm.group(1)}"

    if "asmt-10" in low or "asmt 10" in low or re.search(r"section\s+61\b", low) or "scrutiny of returns" in low:
        return "SCRUTINY_ASMT10", sec or "Section 61"
    if "drc-01b" in low or "drc 01b" in low:
        return "ITC_MISMATCH", sec
    if re.search(r"section\s+74\b", low):
        return "DEMAND_SCN_74", sec or "Section 74"
    if re.search(r"section\s+73\b", low):
        return "DEMAND_SCN_73", sec or "Section 73"
    if "reg-17" in low or "cancellation of registration" in low or re.search(r"section\s+29\b", low):
        return "REGISTRATION_SCN", sec or "Section 29"
    if re.search(r"section\s+65\b", low) or ("audit" in low and "notice" in low):
        return "AUDIT_65", sec
    if "rfd-08" in low or ("refund" in low and ("deficien" in low or "reject" in low)):
        return "REFUND_SCN", sec
    if "gstr-3a" in low or "gstr 3a" in low or "failure to file" in low:
        return "RETURN_DEFAULT", sec
    if "itc" in low and ("mismatch" in low or "gstr-2b" in low or "excess" in low):
        return "ITC_MISMATCH", sec
    return "OTHER", sec


def _amounts(text: str) -> list[Decimal]:
    out = []
    for m in _AMOUNT.finditer(text):
        try:
            out.append(Decimal(m.group(1).replace(",", "")))
        except InvalidOperation:
            continue
    return out


_ASK = re.compile(r"\b(explain|furnish|reconcil|clarif|submit|reply|respond|show\s+cause|provide|justify|why)\w*", re.I)


def find_questions(text: str, limit: int = 6) -> list[str]:
    """The points that ask the taxpayer to do or explain something.

    A numbered item that wraps over several lines is one point, so the text is
    cut at the start of each numbered item and at blank lines, not at every
    line break. When the notice numbers its points, only those are returned;
    otherwise any paragraph that sounds like a request.
    """
    chunks = re.split(r"\n\s*\n|\n(?=\s*(?:\d+[.)]|[-*•])\s)", text)
    numbered = re.compile(r"^\s*\d+[.)]\s")

    # PDF text often loses blank lines, so the last numbered point can run on
    # into the closing paragraphs. Keep sentences until one clearly starts
    # something else.
    closing = re.compile(r"^(amount|you are|if you|please|proper officer|yours|note)\b", re.I)

    def clean(chunk: str) -> str:
        flat = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", " ".join(chunk.split()))
        kept: list[str] = []
        for sentence in re.split(r"(?<=[.;])\s+(?=[A-Z])", flat):
            if kept and closing.match(sentence):
                break
            kept.append(sentence)
        return " ".join(kept)

    pool = [c for c in chunks if numbered.match(c)] or chunks
    questions: list[str] = []
    for chunk in pool:
        line = clean(chunk)
        if 20 <= len(line) <= 500 and _ASK.search(line) and line not in questions:
            questions.append(line)
        if len(questions) >= limit:
            break
    return questions


class MockNoticeAnalyzer(NoticeAnalyzer):
    provider = "mock"
    model = "rule-based"

    def analyse(self, notice_text: str) -> NoticeFacts:
        text = notice_text or ""
        notice_type, section = classify(text)
        dates = find_dates(text)

        # Notice date: the first date in the document, which on a portal-issued
        # notice is the issue date in the header.
        notice_date = dates[0][1] if dates else None

        # Deadline: a date that follows reply/respond/submit language closely.
        deadline: Optional[date] = None
        basis: Optional[str] = None
        for pos, d in dates:
            context = text[max(0, pos - 90):pos].lower()
            if re.search(r"(reply|respond|response|submit|appear|on or before|by|within|last date|due)", context) \
                    and d != notice_date:
                deadline = d
                # The line the date sits on, which is the wording a person
                # will look for in the notice.
                line_start = text.rfind("\n", 0, pos) + 1
                line_end = text.find("\n", pos)
                basis = " ".join(text[line_start:line_end if line_end != -1 else len(text)].split())[:200]
                break
        warnings: list[str] = []
        if deadline is None:
            rel = re.search(r"within\s+(\d{1,3})\s+days?[^.\n]{0,60}", text, re.I)
            if rel:
                basis = " ".join(rel.group(0).split())
                warnings.append(
                    "The notice gives a relative deadline, not a date. It has not been "
                    "calculated: work it out from the date the notice was received."
                )
            else:
                warnings.append("No response deadline could be found in the text.")

        period_from, period_to = find_period(text)
        amounts = _amounts(text)
        gstins = _GSTIN.findall(text.upper())
        ref = _REFERENCE.search(text)

        questions = find_questions(text)
        label = notice_type.replace("_", " ").title()
        return NoticeFacts(
            notice_type=notice_type,
            title=f"{label}" + (f" {ref.group(1)}" if ref else ""),
            reference=ref.group(1) if ref else None,
            section=section,
            notice_date=notice_date,
            response_deadline=deadline,
            deadline_basis=basis,
            period_from=period_from,
            period_to=period_to,
            amount=max(amounts) if amounts else None,
            gstin=gstins[0] if gstins else None,
            issuing_authority=None,
            questions=questions,
            summary=("Read by the local rule-based reader, which matches keywords and "
                     "patterns. Treat every field as a suggestion and check it against "
                     "the notice."),
            confidence="low",
            warnings=warnings,
        )

    def draft_response(self, *, facts: NoticeFacts, notice_text: str,
                       evidence: EvidencePack) -> DraftResult:
        lines: list[str] = []
        cited: list[str] = []
        open_points: list[str] = []

        ref = facts.reference or "[reference not read from the notice]"
        when = facts.notice_date.strftime("%d-%m-%Y") if facts.notice_date else "[date of notice]"
        lines += [
            "To,",
            "The Proper Officer" + (f", {facts.issuing_authority}" if facts.issuing_authority else ""),
            "",
            f"Subject: Reply to notice {ref} dated {when}"
            + (f" under {facts.section}" if facts.section else ""),
            "",
            "Sir/Madam,",
            "",
            "With reference to the above notice, we submit our reply as follows.",
            "",
        ]

        if evidence.items:
            lines.append("Reconciliation position, from our records:")
            for item in evidence.items:
                if item.kind == "RUN_SUMMARY":
                    lines.append(f"- {item.text} [{item.id}]")
                    cited.append(item.id)
            lines.append("")
        else:
            open_points.append("No reconciliation records were available for this notice's period.")
            lines += ["[TO BE COMPLETED BY PRACTITIONER: no reconciliation data was available "
                      "for the period covered by this notice.]", ""]

        questions = facts.questions or ["[The points raised in the notice were not read; "
                                        "list them and reply to each.]"]
        exception_items = [i for i in evidence.items if i.kind == "EXCEPTION"]
        lines.append("Point-wise reply:")
        for n, q in enumerate(questions, start=1):
            lines.append(f"{n}. {q}")
            if exception_items and n == 1:
                lines.append("   The following invoices were flagged in our reconciliation:")
                for item in exception_items:
                    lines.append(f"   - {item.text} [{item.id}]")
                    cited.append(item.id)
            else:
                lines.append("   [TO BE COMPLETED BY PRACTITIONER: explanation and supporting documents.]")
                open_points.append(f"Point {n} needs an explanation from the practitioner.")
        lines += ["", "We request that the above be taken on record, and that we be given "
                      "an opportunity of being heard before any adverse order is passed.",
                  "", "Yours faithfully,", "[Authorised signatory]"]

        seen: list[str] = []
        for c in cited:
            if c not in seen:
                seen.append(c)
        return DraftResult(text="\n".join(lines), cited_evidence_ids=seen, open_points=open_points)
