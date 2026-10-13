"""
GST notices: upload, reading, drafting, approval, filing.

Access: staff only (Associate and above). Notices and drafted replies are the
firm's working papers on a client's dispute with the tax department, so
client-viewer accounts never see them.

Handlers are plain `def`, not `async def`: they call the database and, for
analysis and drafting, an external AI service that can take many seconds.
FastAPI runs plain handlers in a worker thread, so a slow call ties up one
thread rather than the whole event loop.

Every read and write goes through the TenantSession, so a notice, client,
GSTIN or user belonging to another firm is a 404, never a 403 that would
confirm it exists.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile,
)
from pydantic import BaseModel, Field

from app import audit, models
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.notice_ai import NOTICE_TYPES, AnalysisError, NoticeAnalyzer, get_notice_analyzer
from app.ocr import TextExtractor
from app.services import notices as svc
from app.services.notice_evidence import build_evidence
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/notices", tags=["notices"])

Staff = Depends(require_role(Role.ASSOCIATE))


# ---------------------------------------------------------------------------
# Dependencies (overridable in tests)
# ---------------------------------------------------------------------------

def get_analyzer() -> NoticeAnalyzer:
    try:
        return get_notice_analyzer()
    except AnalysisError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def get_optional_analyzer() -> Optional[NoticeAnalyzer]:
    """For upload, where a missing analyser must not stop the file being kept."""
    try:
        return get_notice_analyzer()
    except AnalysisError:
        return None


class Extractors:
    """Injection point for the PDF and OCR readers."""
    def __init__(self, pdf: Optional[TextExtractor] = None, ocr: Optional[TextExtractor] = None):
        self.pdf, self.ocr = pdf, ocr


def get_extractors() -> Extractors:
    return Extractors()


def _meta(request: Request) -> dict:
    return {"ip_address": client_ip(request), "user_agent": user_agent(request)}


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

def _names(tenant: TenantSession, notices: list[models.Notice]) -> dict[str, str]:
    ids = set()
    for n in notices:
        ids |= {n.assigned_to, n.drafted_by, n.submitted_by, n.approved_by, n.filed_by,
                n.closed_by, n.uploaded_by}
    ids.discard(None)
    if not ids:
        return {}
    rows = tenant.query(models.User).filter(models.User.id.in_(ids)).all()
    return {u.id: u.name for u in rows}


def _client_lookup(tenant: TenantSession, notices: list[models.Notice]):
    client_ids = {n.client_id for n in notices}
    gstin_ids = {n.client_gstin_id for n in notices if n.client_gstin_id}
    clients = {c.id: c.name for c in tenant.query(models.Client).filter(
        models.Client.id.in_(client_ids)).all()} if client_ids else {}
    gstins = {g.id: g.gstin for g in tenant.query(models.ClientGSTIN).filter(
        models.ClientGSTIN.id.in_(gstin_ids)).all()} if gstin_ids else {}
    return clients, gstins


def _money(value) -> Optional[str]:
    return str(Decimal(value)) if value is not None else None


def _summary(n: models.Notice, names: dict, clients: dict, gstins: dict, today: date) -> dict:
    days = svc.days_until(n.response_deadline, today)
    return {
        "id": n.id,
        "client_id": n.client_id,
        "client_name": clients.get(n.client_id),
        "client_gstin_id": n.client_gstin_id,
        "gstin": gstins.get(n.client_gstin_id),
        "title": n.title,
        "notice_type": n.notice_type,
        "notice_type_label": NOTICE_TYPES.get(n.notice_type or "", None),
        "notice_reference": n.notice_reference,
        "section": n.section,
        "notice_date": n.notice_date,
        "response_deadline": n.response_deadline,
        "deadline_confirmed": n.deadline_confirmed,
        "days_until_deadline": days if n.status in svc.OPEN_STATUSES else None,
        "urgency": svc.urgency(n, today),
        "period": n.period,
        "period_to": n.period_to,
        "amount_in_question": _money(n.amount_in_question),
        "status": n.status.value,
        "assigned_to": n.assigned_to,
        "assigned_to_name": names.get(n.assigned_to),
        "extraction_status": n.extraction_status.value,
        "analysis_status": n.analysis_status.value,
        "original_filename": n.original_filename,
        "created_at": n.created_at,
        "updated_at": n.updated_at,
    }


def _detail(tenant: TenantSession, n: models.Notice, actor: models.User) -> dict:
    names = _names(tenant, [n])
    clients, gstins = _client_lookup(tenant, [n])
    today = svc.today_ist()
    out = _summary(n, names, clients, gstins, today)
    out.update({
        "ocr_text": n.ocr_text,
        "extraction_method": n.extraction_method,
        "extraction_message": n.extraction_message,
        "page_count": n.page_count,
        "content_type": n.content_type,
        "size_bytes": n.size_bytes,
        "file_available": bool(n.uploaded_file_path),
        "ai_analysis": n.ai_analysis,
        "analysis_error": n.analysis_error,
        "analysed_at": n.analysed_at,
        "drafted_response": n.drafted_response,
        "drafted_by": n.drafted_by, "drafted_by_name": names.get(n.drafted_by),
        "drafted_at": n.drafted_at,
        "submitted_by": n.submitted_by, "submitted_by_name": names.get(n.submitted_by),
        "submitted_at": n.submitted_at,
        "rework_reason": n.rework_reason,
        "approved_by": n.approved_by, "approved_by_name": names.get(n.approved_by),
        "approved_at": n.approved_at,
        "filed_by": n.filed_by, "filed_by_name": names.get(n.filed_by),
        "filed_at": n.filed_at, "filing_reference": n.filing_reference,
        "outcome": n.outcome,
        "closed_by": n.closed_by, "closed_at": n.closed_at,
        "uploaded_by": n.uploaded_by, "uploaded_by_name": names.get(n.uploaded_by),
        "actions": svc.next_actions(n, actor),
        "editable": {
            "facts": n.status in svc.FACTS_EDITABLE,
            "draft": n.status in svc.DRAFT_EDITABLE,
            "deadline": n.status in svc.DEADLINE_EDITABLE,
            "text": n.status in svc.FACTS_EDITABLE,
        },
    })
    return out


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------

class NoticeUpdateIn(BaseModel):
    title: Optional[str] = Field(None, max_length=300)
    notice_type: Optional[str] = Field(None, max_length=100)
    notice_reference: Optional[str] = Field(None, max_length=100)
    section: Optional[str] = Field(None, max_length=100)
    notice_date: Optional[date] = None
    response_deadline: Optional[date] = None
    period: Optional[str] = Field(None, max_length=7)
    period_to: Optional[str] = Field(None, max_length=7)
    amount_in_question: Optional[Decimal] = Field(None, ge=0, max_digits=14, decimal_places=2)
    client_gstin_id: Optional[str] = None
    assigned_to: Optional[str] = None
    confirm_deadline: bool = False


class TextIn(BaseModel):
    text: str = Field(..., min_length=20, max_length=500_000)


class AnalyseIn(BaseModel):
    overwrite: bool = False


class DraftIn(BaseModel):
    text: str = Field(..., max_length=200_000)


class GenerateDraftIn(BaseModel):
    replace: bool = False


class TransitionIn(BaseModel):
    to_status: models.NoticeStatus
    reason: Optional[str] = Field(None, max_length=2000)
    filing_reference: Optional[str] = Field(None, max_length=100)
    filed_at: Optional[date] = None
    outcome: Optional[str] = Field(None, max_length=4000)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

@router.get("/types")
def notice_types(_: models.User = Staff):
    return [{"code": code, "label": label} for code, label in NOTICE_TYPES.items()]


@router.get("/stats")
def notice_stats(_: models.User = Staff, tenant: TenantSession = Depends(get_tenant)):
    """Headline numbers for the notices page."""
    today = svc.today_ist()
    open_rows = tenant.query(models.Notice).filter(
        models.Notice.status.in_(svc.OPEN_STATUSES)).all()
    days = [svc.days_until(n.response_deadline, today) for n in open_rows]
    return {
        "open": len(open_rows),
        "overdue": sum(1 for d in days if d is not None and d < 0),
        "due_within_7_days": sum(1 for d in days if d is not None and 0 <= d <= 7),
        "awaiting_approval": sum(1 for n in open_rows if n.status == models.NoticeStatus.RESPONSE_DRAFTED),
        "unassigned": sum(1 for n in open_rows if n.assigned_to is None),
        "no_deadline": sum(1 for d in days if d is None),
    }


@router.get("")
def list_notices(
    client_id: Optional[str] = Query(None),
    status: Optional[models.NoticeStatus] = Query(None),
    open_only: bool = Query(False, description="Hide filed and closed notices"),
    assigned_to: Optional[str] = Query(None, description="A user id, or 'me'"),
    limit: int = Query(200, ge=1, le=1000),
    actor: models.User = Staff,
    tenant: TenantSession = Depends(get_tenant),
):
    """Notices, nearest deadline first, undated ones last.

    The firm-wide view of open work is `?open_only=true`. A client's history,
    open and closed together, is `?client_id=<id>`.
    """
    q = tenant.query(models.Notice)
    if client_id:
        tenant.get_or_404(models.Client, client_id, "Client not found")
        q = q.filter(models.Notice.client_id == client_id)
    if status:
        q = q.filter(models.Notice.status == status)
    if open_only:
        q = q.filter(models.Notice.status.in_(svc.OPEN_STATUSES))
    if assigned_to:
        q = q.filter(models.Notice.assigned_to == (actor.id if assigned_to == "me" else assigned_to))

    rows = q.order_by(models.Notice.response_deadline.asc().nullslast(),
                      models.Notice.created_at.desc()).limit(limit).all()
    names = _names(tenant, rows)
    clients, gstins = _client_lookup(tenant, rows)
    today = svc.today_ist()
    return [_summary(n, names, clients, gstins, today) for n in rows]


@router.post("", status_code=201)
def upload_notice(
    request: Request,
    file: UploadFile = File(...),
    client_id: str = Form(...),
    client_gstin_id: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    actor: models.User = Staff,
    tenant: TenantSession = Depends(get_tenant),
    analyzer: Optional[NoticeAnalyzer] = Depends(get_optional_analyzer),
    extractors: Extractors = Depends(get_extractors),
):
    """Upload a notice (PDF, PNG or JPEG). It is encrypted, read, and analysed.

    Reading and analysis are best effort: if either fails the notice is still
    saved, with the reason recorded, and the person can retry or type the text
    in. Losing the upload because a downstream step failed would be worse.
    """
    client = tenant.get_or_404(models.Client, client_id, "Client not found")
    gstin = None
    if client_gstin_id:
        gstin = tenant.get(models.ClientGSTIN, client_gstin_id)
        if gstin is None or gstin.client_id != client.id:
            raise HTTPException(404, "GSTIN not found for this client.")

    limit = svc.settings.max_notice_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)          # +1 so an oversize file is seen as such
    meta = _meta(request)

    notice = svc.create_notice(
        tenant, actor, data=data, filename=file.filename or "notice", client=client,
        gstin=gstin, title=title, request_meta=meta, pdf=extractors.pdf, ocr=extractors.ocr,
    )

    if (notice.ocr_text or "").strip() and \
            notice.extraction_status == models.NoticeExtractionStatus.DONE:
        if analyzer is None:
            notice.analysis_status = models.NoticeAnalysisStatus.FAILED
            notice.analysis_error = ("The AI analyser is not configured, so the notice was "
                                     "not read. Set it up and use Re-analyse.")
        else:
            svc.analyse_notice(tenant, notice, analyzer, actor=actor, request_meta=meta)

    tenant.commit()
    tenant.refresh(notice)
    return _detail(tenant, notice, actor)


# ---------------------------------------------------------------------------
# One notice
# ---------------------------------------------------------------------------

def _get(tenant: TenantSession, notice_id: str) -> models.Notice:
    return tenant.get_or_404(models.Notice, notice_id, "Notice not found")


@router.get("/{notice_id}")
def get_notice(notice_id: str, actor: models.User = Staff,
               tenant: TenantSession = Depends(get_tenant)):
    return _detail(tenant, _get(tenant, notice_id), actor)


@router.patch("/{notice_id}")
def update_notice(notice_id: str, payload: NoticeUpdateIn, request: Request,
                  actor: models.User = Staff, tenant: TenantSession = Depends(get_tenant)):
    """Correct the details read from the notice, assign it, or confirm the deadline.

    Only fields present in the request change; sending `null` clears one.
    """
    notice = _get(tenant, notice_id)
    sent = payload.model_fields_set - {"confirm_deadline"}
    changes = {k: getattr(payload, k) for k in sent}
    if not changes and not payload.confirm_deadline:
        raise HTTPException(422, "Nothing to change.")
    svc.update_notice(tenant, notice, actor, changes,
                      confirm_deadline=payload.confirm_deadline, request_meta=_meta(request))
    tenant.commit()
    tenant.refresh(notice)
    return _detail(tenant, notice, actor)


@router.delete("/{notice_id}", status_code=204)
def delete_notice(notice_id: str, request: Request,
                  actor: models.User = Depends(require_role(Role.MANAGER)),
                  tenant: TenantSession = Depends(get_tenant)):
    """Remove a notice uploaded by mistake. It is retained, flagged as deleted,
    with its file and audit trail, rather than destroyed."""
    notice = _get(tenant, notice_id)
    tenant.soft_delete(notice)
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.NOTICE_DELETED, entity_type="Notice", entity_id=notice.id,
        metadata={"status": notice.status.value, "title": notice.title}, **_meta(request),
    )
    tenant.commit()
    return Response(status_code=204)


@router.put("/{notice_id}/text")
def put_text(notice_id: str, payload: TextIn, request: Request,
             actor: models.User = Staff, tenant: TenantSession = Depends(get_tenant)):
    """Type or paste the notice text, for a scan that could not be read."""
    notice = _get(tenant, notice_id)
    svc.set_text(tenant, notice, actor, payload.text, request_meta=_meta(request))
    tenant.commit()
    tenant.refresh(notice)
    return _detail(tenant, notice, actor)


@router.post("/{notice_id}/analyse")
def analyse(notice_id: str, request: Request, payload: Optional[AnalyseIn] = None,
            actor: models.User = Staff, tenant: TenantSession = Depends(get_tenant),
            analyzer: NoticeAnalyzer = Depends(get_analyzer)):
    """Read (or re-read) the notice. Existing edits are kept unless `overwrite`."""
    notice = _get(tenant, notice_id)
    if notice.status not in svc.FACTS_EDITABLE:
        raise HTTPException(409, "A notice cannot be re-analysed after its response is drafted.")
    svc.analyse_notice(tenant, notice, analyzer, actor=actor,
                       overwrite=bool(payload and payload.overwrite), request_meta=_meta(request))
    tenant.commit()
    tenant.refresh(notice)
    return _detail(tenant, notice, actor)


@router.get("/{notice_id}/evidence")
def evidence(notice_id: str, actor: models.User = Staff,
             tenant: TenantSession = Depends(get_tenant)):
    """Exactly the reconciliation records a drafted response would be built from."""
    notice = _get(tenant, notice_id)
    pack = build_evidence(tenant, notice)
    return {
        "gstin": pack.gstin, "periods": pack.periods, "gaps": pack.gaps,
        "items": [{"id": i.id, "kind": i.kind, "text": i.text, "data": i.data} for i in pack.items],
    }


@router.post("/{notice_id}/draft/generate")
def generate_draft(notice_id: str, request: Request, payload: Optional[GenerateDraftIn] = None,
                   actor: models.User = Staff, tenant: TenantSession = Depends(get_tenant),
                   analyzer: NoticeAnalyzer = Depends(get_analyzer)):
    notice = _get(tenant, notice_id)
    svc.generate_draft(tenant, notice, actor, analyzer,
                       replace=bool(payload and payload.replace), request_meta=_meta(request))
    tenant.commit()
    tenant.refresh(notice)
    return _detail(tenant, notice, actor)


@router.put("/{notice_id}/draft")
def put_draft(notice_id: str, payload: DraftIn, request: Request,
              actor: models.User = Staff, tenant: TenantSession = Depends(get_tenant)):
    notice = _get(tenant, notice_id)
    svc.save_draft(tenant, notice, actor, payload.text, request_meta=_meta(request))
    tenant.commit()
    tenant.refresh(notice)
    return _detail(tenant, notice, actor)


@router.post("/{notice_id}/transition")
def transition(notice_id: str, payload: TransitionIn, request: Request,
               actor: models.User = Staff, tenant: TenantSession = Depends(get_tenant)):
    """Move a notice along the pipeline. The server decides whether the move is
    allowed; the notice's `actions` list says in advance which moves are."""
    notice = _get(tenant, notice_id)
    svc.transition(
        tenant, notice, actor, payload.to_status, reason=payload.reason,
        filing_reference=payload.filing_reference, filed_at=payload.filed_at,
        outcome=payload.outcome, request_meta=_meta(request),
    )
    tenant.commit()
    tenant.refresh(notice)
    return _detail(tenant, notice, actor)


@router.get("/{notice_id}/file")
def download_file(notice_id: str, request: Request, actor: models.User = Staff,
                  tenant: TenantSession = Depends(get_tenant)):
    """The original upload, decrypted. Every download is audited."""
    notice = _get(tenant, notice_id)
    data = svc.load_file(tenant, notice)
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.NOTICE_FILE_DOWNLOADED, entity_type="Notice", entity_id=notice.id,
        metadata={"filename": notice.original_filename}, **_meta(request),
    )
    tenant.commit()
    filename = notice.original_filename or "notice"
    return Response(
        content=data, media_type=notice.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{notice_id}/history")
def history(notice_id: str, actor: models.User = Staff,
            tenant: TenantSession = Depends(get_tenant)):
    """Who did what to this notice, oldest first, from the audit log."""
    notice = _get(tenant, notice_id)
    rows = (
        tenant.query(models.AuditLog)
        .filter(models.AuditLog.entity_type == "Notice", models.AuditLog.entity_id == notice.id)
        .order_by(models.AuditLog.created_at.asc(), models.AuditLog.id.asc())
        .all()
    )
    ids = {r.user_id for r in rows if r.user_id}
    names = {u.id: u.name for u in tenant.query(models.User).filter(
        models.User.id.in_(ids)).all()} if ids else {}
    return [
        {"id": r.id, "action": r.action, "user_id": r.user_id,
         "user_name": names.get(r.user_id), "created_at": r.created_at, "details": r.meta}
        for r in rows
    ]
