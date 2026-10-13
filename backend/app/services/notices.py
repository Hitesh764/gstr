"""
The notice workflow: intake, reading, drafting, approval, filing, deadline alerts.

Plain functions over a TenantSession. Routers stay thin, and the Celery alert
job calls `send_deadline_alerts` directly.

Pipeline (one direction, plus a single send-back):

    RECEIVED -> UNDER_REVIEW -> RESPONSE_DRAFTED -> PARTNER_APPROVED -> FILED -> CLOSED
                     ^                 |
                     +--- send back ---+

Rules that carry the weight, each enforced here rather than left to the screen:

* **Drafter is not approver.** Whoever last changed the response text
  (`drafted_by`) or sent it for approval (`submitted_by`) can never approve it.
  This is checked against the stored ids, not the caller's role, so a partner
  who wrote the reply still needs a second partner to sign it off.
* **A deadline must be confirmed by a person** before a response can be
  submitted. The analyser's reading of a date is a suggestion; a wrong
  deadline is the one error in this module that cannot be undone.
* **A draft cites real records only.** See `check_grounding`.
* **Nothing fails silently.** Every change is written to the audit log, and
  alerts go out at 7, 3 and 1 days, on the day, and once when overdue.
"""
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import audit, models
from app.audit import AuditAction
from app.config import settings
from app.deps import role_rank
from app.notice_ai import NOTICE_TYPES, AnalysisError, NoticeAnalyzer, NoticeFacts
from app.notice_ai.base import DraftResult, EvidencePack
from app.notifications import NotificationType, notify, notify_many
from app.ocr import ExtractionStatus, TextExtractor, extract_text, sniff_content_type
from app.services import consent as consent_svc
from app.services.notice_evidence import build_evidence
from app.storage import StorageError, get_storage
from app.tenancy import TenantSession

logger = logging.getLogger("gstr2b.notices")

Status = models.NoticeStatus
Role = models.UserRole

OPEN_STATUSES = (Status.RECEIVED, Status.UNDER_REVIEW, Status.RESPONSE_DRAFTED,
                 Status.PARTNER_APPROVED)

TRANSITIONS: dict[Status, set[Status]] = {
    Status.RECEIVED: {Status.UNDER_REVIEW},
    Status.UNDER_REVIEW: {Status.RESPONSE_DRAFTED},
    Status.RESPONSE_DRAFTED: {Status.PARTNER_APPROVED, Status.UNDER_REVIEW},
    Status.PARTNER_APPROVED: {Status.FILED},
    Status.FILED: {Status.CLOSED},
    Status.CLOSED: set(),
}

# Minimum role to move a notice INTO each status.
MIN_ROLE_FOR = {
    Status.UNDER_REVIEW: Role.ASSOCIATE,
    Status.RESPONSE_DRAFTED: Role.ASSOCIATE,
    Status.PARTNER_APPROVED: Role.PARTNER,
    Status.FILED: Role.MANAGER,
    Status.CLOSED: Role.MANAGER,
}

# Who may edit what, by status.
FACTS_EDITABLE = (Status.RECEIVED, Status.UNDER_REVIEW)
DRAFT_EDITABLE = (Status.UNDER_REVIEW,)
DEADLINE_EDITABLE = (Status.RECEIVED, Status.UNDER_REVIEW,
                     Status.RESPONSE_DRAFTED, Status.PARTNER_APPROVED)

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def today_ist() -> date:
    """Deadlines are Indian calendar dates; the server clock is UTC."""
    return consent_svc.to_ist(datetime.utcnow()).date()


def days_until(deadline: Optional[date], today: Optional[date] = None) -> Optional[int]:
    return (deadline - (today or today_ist())).days if deadline else None


def urgency(notice: models.Notice, today: Optional[date] = None) -> str:
    if notice.status not in OPEN_STATUSES:
        return "none"
    days = days_until(notice.response_deadline, today)
    if days is None:
        return "none"
    if days < 0:
        return "overdue"
    if days == 0:
        return "due_today"
    if days <= 3:
        return "critical"
    if days <= 7:
        return "warning"
    return "ok"


def _partners(db: Session, firm_id: str) -> list[str]:
    rows = (
        db.query(models.User)
        .filter(models.User.firm_id == firm_id,
                models.User.role.in_((Role.PARTNER, Role.OWNER)),
                models.User.is_active.is_(True), models.User.deleted_at.is_(None))
        .all()
    )
    return [u.id for u in rows]


def _managers_and_up(db: Session, firm_id: str) -> list[str]:
    rows = (
        db.query(models.User)
        .filter(models.User.firm_id == firm_id,
                models.User.role.in_((Role.MANAGER, Role.PARTNER, Role.OWNER)),
                models.User.is_active.is_(True), models.User.deleted_at.is_(None))
        .all()
    )
    return [u.id for u in rows]


def _label(notice: models.Notice) -> str:
    return notice.title or notice.notice_reference or "a notice"


def _link(notice: models.Notice) -> str:
    return f"/notices/{notice.id}"


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

def validate_upload(data: bytes) -> str:
    """Returns the real content type of the bytes, or raises."""
    if not data:
        raise HTTPException(422, "The uploaded file is empty.")
    limit = settings.max_notice_upload_mb * 1024 * 1024
    if len(data) > limit:
        raise HTTPException(
            413, f"File is {len(data) / 1_048_576:.1f} MB, over the "
                 f"{settings.max_notice_upload_mb} MB limit.")
    content_type = sniff_content_type(data)
    if content_type is None:
        raise HTTPException(415, "Unsupported file. Upload a PDF, PNG or JPEG.")
    return content_type


def run_extraction(
    notice: models.Notice, data: bytes, *,
    pdf: Optional[TextExtractor] = None, ocr: Optional[TextExtractor] = None,
) -> None:
    result = extract_text(data, notice.content_type or "", pdf=pdf, ocr=ocr)
    notice.page_count = result.pages
    notice.extraction_method = result.method
    notice.extraction_message = " ".join([m for m in [result.message, *result.warnings] if m]) or None
    if result.status == ExtractionStatus.DONE:
        notice.extraction_status = models.NoticeExtractionStatus.DONE
        notice.ocr_text = result.text
    elif result.status == ExtractionStatus.NEEDS_OCR:
        notice.extraction_status = models.NoticeExtractionStatus.NEEDS_OCR
        notice.ocr_text = result.text or None
    else:
        notice.extraction_status = models.NoticeExtractionStatus.FAILED


def create_notice(
    tenant: TenantSession, actor: models.User, *, data: bytes, filename: str,
    client: models.Client, gstin: Optional[models.ClientGSTIN], title: Optional[str],
    request_meta: Optional[dict] = None,
    pdf: Optional[TextExtractor] = None, ocr: Optional[TextExtractor] = None,
) -> models.Notice:
    content_type = validate_upload(data)
    storage = get_storage()
    try:
        key = storage.save(firm_id=tenant.firm_id, category="notices", data=data)
    except StorageError as exc:
        raise HTTPException(503, f"The file could not be stored: {exc}") from exc

    try:
        if gstin is None:
            # A client with exactly one GSTIN needs no choice made.
            gstins = tenant.query(models.ClientGSTIN).filter(
                models.ClientGSTIN.client_id == client.id).all()
            if len(gstins) == 1:
                gstin = gstins[0]

        notice = models.Notice(
            client_id=client.id, client_gstin_id=gstin.id if gstin else None,
            title=(title or "").strip() or None,
            uploaded_file_path=key, original_filename=(filename or "notice")[:400],
            content_type=content_type, size_bytes=len(data),
            file_sha256=hashlib.sha256(data).hexdigest(),
            status=Status.RECEIVED, uploaded_by=actor.id,
        )
        tenant.add(notice)
        run_extraction(notice, data, pdf=pdf, ocr=ocr)
        tenant.flush()
        audit.record(
            tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.NOTICE_UPLOADED, entity_type="Notice", entity_id=notice.id,
            metadata={"client_id": client.id, "filename": notice.original_filename,
                      "size_bytes": len(data), "sha256": notice.file_sha256,
                      "extraction": notice.extraction_status.value},
            **(request_meta or {}),
        )
    except Exception:
        # Do not leave an encrypted file behind that no row points at.
        try:
            storage.delete(key, firm_id=tenant.firm_id)
        except StorageError:
            pass
        raise
    return notice


def load_file(tenant: TenantSession, notice: models.Notice) -> bytes:
    if not notice.uploaded_file_path:
        raise HTTPException(404, "This notice has no stored file.")
    try:
        return get_storage().load(notice.uploaded_file_path, firm_id=tenant.firm_id)
    except StorageError as exc:
        raise HTTPException(502, str(exc)) from exc


# ---------------------------------------------------------------------------
# Reading the notice
# ---------------------------------------------------------------------------

def _facts_from_notice(tenant: TenantSession, notice: models.Notice) -> NoticeFacts:
    """The working, person-reviewed values, for the drafter."""
    prior = ((notice.ai_analysis or {}).get("facts") or {})
    gstin = tenant.get(models.ClientGSTIN, notice.client_gstin_id) if notice.client_gstin_id else None
    return NoticeFacts(
        notice_type=notice.notice_type or "OTHER", title=notice.title,
        reference=notice.notice_reference, section=notice.section,
        notice_date=notice.notice_date, response_deadline=notice.response_deadline,
        deadline_basis=prior.get("deadline_basis"),
        period_from=notice.period, period_to=notice.period_to,
        amount=notice.amount_in_question, gstin=gstin.gstin if gstin else None,
        issuing_authority=prior.get("issuing_authority"),
        questions=list(prior.get("questions") or []),
    )


def _apply_facts(tenant: TenantSession, notice: models.Notice, facts: NoticeFacts,
                 analyzer: NoticeAnalyzer, *, overwrite: bool) -> None:
    """Copy what was read into the working columns, without trampling a person's edits.

    A field is filled only when it is empty, unless `overwrite` is set. The
    complete original reading is kept in `ai_analysis` either way.
    """
    def put(attr: str, value):
        if value in (None, ""):
            return
        if overwrite or getattr(notice, attr) in (None, ""):
            setattr(notice, attr, value)

    notice_type = facts.notice_type if facts.notice_type in NOTICE_TYPES else "OTHER"
    put("notice_type", notice_type)
    put("title", facts.title)
    put("notice_reference", facts.reference)
    put("section", facts.section)
    put("notice_date", facts.notice_date)
    put("period", facts.period_from)
    put("period_to", facts.period_to)
    put("amount_in_question", facts.amount)

    if facts.response_deadline and (overwrite or notice.response_deadline is None):
        notice.response_deadline = facts.response_deadline
        notice.deadline_confirmed = False     # a person must check it against the notice

    if notice.period and notice.period_to and notice.period_to < notice.period:
        notice.period_to = None

    facts_json = facts.to_json()
    if facts.gstin:
        if notice.client_gstin_id is None:
            match = tenant.query(models.ClientGSTIN).filter(
                models.ClientGSTIN.client_id == notice.client_id,
                models.ClientGSTIN.gstin == facts.gstin).first()
            if match is not None:
                notice.client_gstin_id = match.id
        else:
            linked = tenant.get(models.ClientGSTIN, notice.client_gstin_id)
            if linked is not None and linked.gstin != facts.gstin:
                facts_json["warnings"].append(
                    f"The notice names GSTIN {facts.gstin}, but this notice is linked to "
                    f"{linked.gstin}. Check that the right GSTIN is selected.")

    previous = dict(notice.ai_analysis or {})
    previous.update({
        "provider": analyzer.provider, "model": analyzer.model,
        "analysed_at": datetime.utcnow().isoformat(), "facts": facts_json,
    })
    notice.ai_analysis = previous          # reassigned so the JSON change is detected
    notice.analysis_status = models.NoticeAnalysisStatus.DONE
    notice.analysis_error = None
    notice.analysed_at = datetime.utcnow()


def analyse_notice(
    tenant: TenantSession, notice: models.Notice, analyzer: NoticeAnalyzer, *,
    actor: models.User, overwrite: bool = False, request_meta: Optional[dict] = None,
) -> bool:
    """Read the notice text. Returns True on success.

    A failure is recorded on the notice and reported to the caller as False;
    it never raises, so a provider outage cannot fail an upload that has
    already stored the file.
    """
    text = (notice.ocr_text or "").strip()
    if not text:
        raise HTTPException(
            409, "There is no text to analyse. If the file is a scan, paste the text of the "
                 "notice in first.")
    if len(text) > settings.notice_ai_max_input_chars:
        notice.analysis_status = models.NoticeAnalysisStatus.FAILED
        notice.analysis_error = (
            f"The notice text is {len(text):,} characters, over the "
            f"{settings.notice_ai_max_input_chars:,} limit. It was not sent, because "
            "cutting it short could drop the deadline.")
        return False

    try:
        facts = analyzer.analyse(text)
    except AnalysisError as exc:
        notice.analysis_status = models.NoticeAnalysisStatus.FAILED
        notice.analysis_error = str(exc)
        audit.record(
            tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.NOTICE_ANALYSED, entity_type="Notice", entity_id=notice.id,
            metadata={"ok": False, "code": exc.code, "provider": analyzer.provider},
            **(request_meta or {}),
        )
        return False

    _apply_facts(tenant, notice, facts, analyzer, overwrite=overwrite)
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.NOTICE_ANALYSED, entity_type="Notice", entity_id=notice.id,
        metadata={"ok": True, "provider": analyzer.provider, "model": analyzer.model,
                  "type": notice.notice_type, "confidence": facts.confidence,
                  "overwrite": overwrite},
        **(request_meta or {}),
    )
    if notice.assigned_to and notice.assigned_to != actor.id:
        notify(tenant.db, user_id=notice.assigned_to, firm_id=tenant.firm_id,
               event_type=NotificationType.NOTICE_ANALYSED,
               title="A notice has been read",
               message=f"{_label(notice)} was read and classified. Check the extracted details.",
               link=_link(notice), entity_type="Notice", entity_id=notice.id)
    return True


def set_text(tenant: TenantSession, notice: models.Notice, actor: models.User, text: str,
             request_meta: Optional[dict] = None) -> None:
    """Replace the notice text by hand: the way through for a scan that OCR
    could not read, or to correct a misread figure."""
    if notice.status not in FACTS_EDITABLE:
        raise HTTPException(409, "The notice text can no longer be edited at this stage.")
    notice.ocr_text = text
    notice.extraction_status = models.NoticeExtractionStatus.MANUAL
    notice.extraction_method = "MANUAL"
    notice.extraction_message = f"Text entered by {actor.name}."
    # The reading of the old text no longer describes this one.
    notice.analysis_status = models.NoticeAnalysisStatus.NOT_RUN
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.NOTICE_TEXT_EDITED, entity_type="Notice", entity_id=notice.id,
        metadata={"characters": len(text)}, **(request_meta or {}),
    )


# ---------------------------------------------------------------------------
# Editing the facts
# ---------------------------------------------------------------------------

EDITABLE_FIELDS = ("title", "notice_type", "notice_reference", "section", "notice_date",
                   "response_deadline", "period", "period_to", "amount_in_question",
                   "client_gstin_id", "assigned_to")


def update_notice(tenant: TenantSession, notice: models.Notice, actor: models.User,
                  changes: dict, *, confirm_deadline: bool = False,
                  request_meta: Optional[dict] = None) -> None:
    """Apply a person's corrections. `changes` holds only the fields sent."""
    touched_facts = [k for k in changes if k not in ("response_deadline", "assigned_to")]
    if touched_facts and notice.status not in FACTS_EDITABLE:
        raise HTTPException(
            409, "The details of a notice are locked once a response has been drafted. "
                 "Send it back for review to change them.")
    if "response_deadline" in changes and notice.status not in DEADLINE_EDITABLE:
        raise HTTPException(409, "A filed or closed notice's deadline cannot be changed.")

    if changes.get("notice_type") is not None and changes["notice_type"] not in NOTICE_TYPES:
        raise HTTPException(422, f"Unknown notice type '{changes['notice_type']}'.")
    for key in ("period", "period_to"):
        if changes.get(key) and not PERIOD_RE.match(changes[key]):
            raise HTTPException(422, f"{key} must be YYYY-MM, for example 2025-03.")
    period = changes.get("period", notice.period)
    period_to = changes.get("period_to", notice.period_to)
    if period and period_to and period_to < period:
        raise HTTPException(422, "The last period cannot be before the first.")

    if changes.get("client_gstin_id"):
        gstin = tenant.get(models.ClientGSTIN, changes["client_gstin_id"])
        if gstin is None or gstin.client_id != notice.client_id:
            # Same answer for "not yours" and "not this client's": no probing.
            raise HTTPException(404, "GSTIN not found for this client.")
    if changes.get("assigned_to"):
        assignee = tenant.get(models.User, changes["assigned_to"])
        if assignee is None or not assignee.is_active:
            raise HTTPException(404, "User not found in this firm.")
        if role_rank(assignee.role) < role_rank(Role.ASSOCIATE):
            raise HTTPException(422, "Notices can only be assigned to staff, not client viewers.")

    before = {k: getattr(notice, k) for k in changes}
    for key, value in changes.items():
        setattr(notice, key, value)
    if "response_deadline" in changes:
        notice.deadline_confirmed = changes["response_deadline"] is not None
    if confirm_deadline:
        if notice.response_deadline is None:
            raise HTTPException(422, "There is no deadline to confirm.")
        notice.deadline_confirmed = True

    def show(v):
        return v.isoformat() if isinstance(v, (date, datetime)) else (str(v) if isinstance(v, Decimal) else v)

    diff = {k: {"from": show(before[k]), "to": show(getattr(notice, k))}
            for k in changes if before[k] != getattr(notice, k)}
    if diff or confirm_deadline:
        audit.record(
            tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.NOTICE_UPDATED, entity_type="Notice", entity_id=notice.id,
            metadata={"changes": diff, "deadline_confirmed": notice.deadline_confirmed},
            **(request_meta or {}),
        )
    if "assigned_to" in diff:
        audit.record(
            tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.NOTICE_ASSIGNED, entity_type="Notice", entity_id=notice.id,
            metadata=diff["assigned_to"], **(request_meta or {}),
        )
        new = changes["assigned_to"]
        if new and new != actor.id:
            notify(tenant.db, user_id=new, firm_id=tenant.firm_id,
                   event_type=NotificationType.NOTICE_ASSIGNED,
                   title="A notice was assigned to you",
                   message=f"{actor.name} assigned you {_label(notice)}"
                           + (f", due {notice.response_deadline:%d-%m-%Y}."
                              if notice.response_deadline else "."),
                   link=_link(notice), entity_type="Notice", entity_id=notice.id)


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

_MARKER = re.compile(r"\[(E\d+)\]")
_AMOUNT = re.compile(r"(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d{1,2})?)", re.I)


@dataclass
class GroundingReport:
    unknown_ids: list[str] = field(default_factory=list)      # cited but never supplied
    unverified_amounts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unknown_ids


def _amounts_in(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in _AMOUNT.finditer(text or ""):
        try:
            out.add(Decimal(m.group(1).replace(",", "")).quantize(Decimal("0.01")))
        except InvalidOperation:
            continue
    return out


def check_grounding(draft: DraftResult, evidence: EvidencePack, facts: NoticeFacts,
                    notice_text: str) -> GroundingReport:
    """Verify the draft against what it was given, instead of trusting it.

    * Every citation, in the list and in the letter's `[E3]` markers, must be an
      evidence id that actually exists. A citation that does not is proof the
      model made something up, and the draft is rejected.
    * Every rupee amount in the letter should appear in the evidence, the
      notice or the confirmed facts. One that does not (an invented figure, or
      a total the model computed) is flagged for the reviewer, not rejected,
      because a sum of two real invoices is legitimate.
    """
    valid = evidence.ids
    cited = set(draft.cited_evidence_ids) | set(_MARKER.findall(draft.text))
    report = GroundingReport(unknown_ids=sorted(c for c in cited if c not in valid))

    known: set[Decimal] = set()
    for item in evidence.items:
        known |= _amounts_in(item.text)
    known |= _amounts_in(notice_text)
    if facts.amount is not None:
        known.add(Decimal(facts.amount).quantize(Decimal("0.01")))
    for amount in sorted(_amounts_in(draft.text)):
        if amount not in known:
            report.unverified_amounts.append(f"{amount:,.2f}")
    return report


def generate_draft(
    tenant: TenantSession, notice: models.Notice, actor: models.User,
    analyzer: NoticeAnalyzer, *, replace: bool = False,
    request_meta: Optional[dict] = None,
) -> dict:
    """Ask the analyser for a first-pass reply built from real records."""
    if notice.status not in DRAFT_EDITABLE:
        raise HTTPException(409, "Start the review before drafting a response.")
    text = (notice.ocr_text or "").strip()
    if not text:
        raise HTTPException(409, "There is no notice text to answer. Paste it in first.")
    if notice.drafted_response and notice.drafted_response.strip() and not replace:
        raise HTTPException(
            409, "A draft already exists. Generating a new one would replace it; "
                 "confirm that you want this.")

    facts = _facts_from_notice(tenant, notice)
    evidence = build_evidence(tenant, notice)
    try:
        result = analyzer.draft_response(facts=facts, notice_text=text, evidence=evidence)
    except AnalysisError as exc:
        raise HTTPException(502 if exc.retryable else 422, str(exc)) from exc

    report = check_grounding(result, evidence, facts, text)
    if not report.ok:
        logger.warning("Draft for notice %s rejected: cited unknown evidence %s",
                       notice.id, report.unknown_ids)
        raise HTTPException(
            502, "The generated draft cited records that do not exist "
                 f"({', '.join(report.unknown_ids)}), so it was discarded. Try again.")
    if not result.text.strip():
        raise HTTPException(502, "The analyser returned an empty draft. Try again.")

    now = datetime.utcnow()
    notice.drafted_response = result.text
    notice.drafted_by = actor.id
    notice.drafted_at = now
    meta = dict(notice.ai_analysis or {})
    meta["draft"] = {
        "provider": analyzer.provider, "model": analyzer.model,
        "generated_at": now.isoformat(), "generated_by": actor.id,
        "cited_evidence_ids": result.cited_evidence_ids,
        "open_points": result.open_points,
        "unverified_amounts": report.unverified_amounts,
        "evidence_gaps": evidence.gaps,
        "evidence_count": len(evidence.items),
    }
    notice.ai_analysis = meta
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.NOTICE_DRAFT_GENERATED, entity_type="Notice", entity_id=notice.id,
        metadata={"provider": analyzer.provider, "model": analyzer.model,
                  "evidence_items": len(evidence.items), "cited": result.cited_evidence_ids,
                  "unverified_amounts": report.unverified_amounts, "replaced": replace},
        **(request_meta or {}),
    )
    return meta["draft"]


def save_draft(tenant: TenantSession, notice: models.Notice, actor: models.User,
               text: str, request_meta: Optional[dict] = None) -> None:
    if notice.status not in DRAFT_EDITABLE:
        raise HTTPException(
            409, "The response can only be edited while the notice is under review. "
                 "Send it back for review to change it.")
    notice.drafted_response = text
    notice.drafted_by = actor.id
    notice.drafted_at = datetime.utcnow()
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.NOTICE_DRAFT_EDITED, entity_type="Notice", entity_id=notice.id,
        metadata={"characters": len(text)}, **(request_meta or {}),
    )


# ---------------------------------------------------------------------------
# The approval pipeline
# ---------------------------------------------------------------------------

def blocked_reason(notice: models.Notice, actor: models.User, to: Status) -> Optional[str]:
    """Why `actor` cannot move this notice to `to` right now, or None if they can.

    The single source of truth for the pipeline's rules: `transition` enforces
    it and the notice payload publishes it, so the screen never offers a button
    the server would refuse.
    """
    if to not in TRANSITIONS[notice.status]:
        return f"Not possible from {notice.status.value.replace('_', ' ').title()}."

    if to == Status.UNDER_REVIEW and notice.status == Status.RESPONSE_DRAFTED:
        is_partner = role_rank(actor.role) >= role_rank(Role.PARTNER)
        is_author = actor.id in (notice.submitted_by, notice.drafted_by)
        if not (is_partner or is_author):
            return "Only a partner, or the person who submitted the response, can send it back."
        return None

    if role_rank(actor.role) < role_rank(MIN_ROLE_FOR[to]):
        return f"Needs {MIN_ROLE_FOR[to].value.title()} or above."

    if to == Status.RESPONSE_DRAFTED:
        if not (notice.drafted_response or "").strip():
            return "Write or generate a response first."
        if notice.response_deadline is None:
            return "Set the response deadline first."
        if not notice.deadline_confirmed:
            return "Confirm the response deadline against the notice first."

    if to == Status.PARTNER_APPROVED:
        if actor.id in (notice.drafted_by, notice.submitted_by):
            return ("You wrote or submitted this response, so someone else must approve it.")
    return None


def next_actions(notice: models.Notice, actor: models.User) -> list[dict]:
    actions = []
    for to in sorted(TRANSITIONS[notice.status], key=lambda s: s.value):
        why = blocked_reason(notice, actor, to)
        actions.append({"to": to.value, "allowed": why is None, "blocked_reason": why})
    return actions


def transition(
    tenant: TenantSession, notice: models.Notice, actor: models.User, to: Status, *,
    reason: Optional[str] = None, filing_reference: Optional[str] = None,
    filed_at: Optional[date] = None, outcome: Optional[str] = None,
    request_meta: Optional[dict] = None,
) -> None:
    reason_text = blocked_reason(notice, actor, to)
    if reason_text:
        code = 409 if to not in TRANSITIONS[notice.status] else 403
        if to == Status.RESPONSE_DRAFTED and role_rank(actor.role) >= role_rank(Role.ASSOCIATE):
            code = 422
        raise HTTPException(code, reason_text)

    came_from = notice.status
    now = datetime.utcnow()

    if to == Status.UNDER_REVIEW and came_from == Status.RECEIVED:
        if notice.assigned_to is None:
            notice.assigned_to = actor.id

    elif to == Status.RESPONSE_DRAFTED:
        notice.submitted_by = actor.id
        notice.submitted_at = now
        notice.rework_reason = None

    elif to == Status.UNDER_REVIEW and came_from == Status.RESPONSE_DRAFTED:
        by_approver = actor.id not in (notice.submitted_by, notice.drafted_by)
        if by_approver and not (reason or "").strip():
            raise HTTPException(422, "Give a reason when sending a response back.")
        notice.rework_reason = (reason or "").strip() or None
        notice.submitted_by = None
        notice.submitted_at = None

    elif to == Status.PARTNER_APPROVED:
        notice.approved_by = actor.id
        notice.approved_at = now

    elif to == Status.FILED:
        filed_on = filed_at or today_ist()
        if filed_on > today_ist():
            raise HTTPException(422, "The filing date cannot be in the future.")
        notice.filed_by = actor.id
        notice.filed_at = filed_on
        notice.filing_reference = (filing_reference or "").strip() or None

    elif to == Status.CLOSED:
        if not (outcome or "").strip():
            raise HTTPException(422, "Record the outcome before closing the notice.")
        notice.outcome = outcome.strip()
        notice.closed_by = actor.id
        notice.closed_at = now

    notice.status = to
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.NOTICE_STATUS_CHANGED, entity_type="Notice", entity_id=notice.id,
        metadata={"from": came_from.value, "to": to.value, "reason": reason,
                  "filing_reference": filing_reference, "outcome": outcome},
        **(request_meta or {}),
    )
    _notify_transition(tenant, notice, actor, came_from, to, reason)


def _notify_transition(tenant: TenantSession, notice: models.Notice, actor: models.User,
                       came_from: Status, to: Status, reason: Optional[str]) -> None:
    db, firm_id = tenant.db, tenant.firm_id
    common = dict(firm_id=firm_id, link=_link(notice), entity_type="Notice", entity_id=notice.id)

    if to == Status.RESPONSE_DRAFTED:
        notify_many(
            db, user_ids=[p for p in _partners(db, firm_id) if p != actor.id],
            event_type=NotificationType.NOTICE_NEEDS_APPROVAL,
            title="A notice response needs your approval",
            message=f"{actor.name} submitted a response to {_label(notice)}"
                    + (f", due {notice.response_deadline:%d-%m-%Y}." if notice.response_deadline else "."),
            **common)

    elif to == Status.PARTNER_APPROVED:
        notify_many(
            db, user_ids=[u for u in {notice.submitted_by, notice.drafted_by, notice.assigned_to}
                          if u and u != actor.id],
            event_type=NotificationType.NOTICE_APPROVED,
            title="A notice response was approved",
            message=f"{actor.name} approved the response to {_label(notice)}. It is ready to file.",
            **common)

    elif to == Status.UNDER_REVIEW and came_from == Status.RESPONSE_DRAFTED:
        targets = {notice.drafted_by, notice.assigned_to} - {actor.id, None}
        notify_many(
            db, user_ids=list(targets),
            event_type=NotificationType.NOTICE_SENT_BACK,
            title="A notice response was sent back",
            message=f"{actor.name} returned the response to {_label(notice)}."
                    + (f" Reason: {reason}" if reason else ""),
            **common)


# ---------------------------------------------------------------------------
# Deadline alerts
# ---------------------------------------------------------------------------

@dataclass
class AlertReport:
    scanned: int = 0
    alerted: int = 0        # notifications created
    notices: int = 0        # distinct notices that produced at least one


def alert_days() -> list[int]:
    """Alert points, ascending. Configured points, plus 'due today' (0) and
    'overdue' (-1), which are always on: silence about a missed deadline is the
    failure this exists to prevent."""
    configured = {int(x) for x in settings.notice_alert_days.replace(" ", "").split(",") if x}
    return sorted(configured | {0, -1})


def alert_point(days_left: int, points: Optional[list[int]] = None) -> Optional[int]:
    """The alert this many days out has reached: the tightest point at or beyond it."""
    for point in (points or alert_days()):
        if days_left <= point:
            return point
    return None


def _alert_text(point: int, days_left: int, notice: models.Notice) -> tuple[str, str]:
    """Wording for an alert. Uses the real days remaining, not the alert point:
    a notice uploaded with two days left crosses the 3-day point, and telling
    someone it is due in 3 days would be wrong."""
    label = _label(notice)
    when = f"{notice.response_deadline:%d-%m-%Y}"
    if point < 0:
        late = -days_left
        return (f"A notice response is {late} day{'s' if late != 1 else ''} overdue",
                f"The response to {label} was due on {when} and has not been filed.")
    if point == 0:
        return ("A notice response is due today",
                f"The response to {label} is due today ({when}).")
    unit = "day" if days_left == 1 else "days"
    return (f"A notice response is due in {days_left} {unit}",
            f"The response to {label} is due on {when}, and it is still "
            f"{notice.status.value.replace('_', ' ').lower()}.")


def send_deadline_alerts(db: Session, today: Optional[date] = None) -> AlertReport:
    """Notify about open notices whose deadlines have reached an alert point.

    Cross-tenant by nature, like the GSTR-2B scheduler: it walks every firm's
    open notices, but each notification is created for a user of the notice's
    own firm. Idempotent: each (notice, deadline, alert point) notifies a
    person once, so running it twice, or every day, sends nothing repeated.
    Changing a deadline makes a new set of alerts due, as it should.
    """
    today = today or today_ist()
    points = alert_days()
    report = AlertReport()

    rows = (
        db.query(models.Notice)
        .filter(models.Notice.status.in_(OPEN_STATUSES),
                models.Notice.deleted_at.is_(None),
                models.Notice.response_deadline.isnot(None))
        .all()
    )
    for notice in rows:
        report.scanned += 1
        days_left = (notice.response_deadline - today).days
        point = alert_point(days_left, points)
        if point is None:
            continue

        recipients: list[str] = []
        assignee = db.get(models.User, notice.assigned_to) if notice.assigned_to else None
        if assignee and assignee.is_active and assignee.deleted_at is None:
            recipients.append(assignee.id)
        else:
            client = db.get(models.Client, notice.client_id)
            owner = db.get(models.User, client.assigned_associate_id) \
                if client and client.assigned_associate_id else None
            if owner and owner.is_active and owner.deleted_at is None:
                recipients.append(owner.id)
        # Escalate close to the deadline, or when nobody owns the notice.
        if point <= 1 or not recipients:
            recipients += _managers_and_up(db, notice.firm_id)

        title, message = _alert_text(point, days_left, notice)
        entity_id = f"{notice.id}:{notice.response_deadline.isoformat()}:{point}"
        made = 0
        for user_id in dict.fromkeys(recipients):
            row = notify(db, user_id=user_id, firm_id=notice.firm_id,
                         event_type=NotificationType.NOTICE_DEADLINE_APPROACHING,
                         title=title, message=message, link=_link(notice),
                         entity_type="Notice", entity_id=entity_id, once=True)
            made += row is not None
        report.alerted += made
        report.notices += 1 if made else 0
    db.commit()
    return report
