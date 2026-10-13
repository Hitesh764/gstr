"""Approval-gated MRL and working-paper workflows and final audit-file export."""
from datetime import datetime, timezone
from io import BytesIO
import json
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.services.caro_responses import build_source_snapshot
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/audit-engagements", tags=["audit-documents"])


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _engagement(tenant, engagement_id):
    return tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")


def _assigned_partner(engagement, actor):
    if actor.id != engagement.engagement_partner_id or actor.role != models.UserRole.PARTNER:
        raise HTTPException(403, "Only the assigned engagement partner may approve or finalize this artifact.")


def _audit(tenant, request, actor, action, entity_type, entity_id, metadata):
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id, action=action,
                 entity_type=entity_type, entity_id=entity_id, metadata=metadata,
                 ip_address=client_ip(request), user_agent=user_agent(request))


def _mrl_template(engagement):
    basis = engagement.materiality_basis or "not recorded"
    amount = str(engagement.materiality_amount) if engagement.materiality_amount is not None else "not confirmed"
    return (f"SYSTEM DRAFT — Management Representation Letter | FY {engagement.financial_year}\n"
            f"Engagement materiality recorded in system: ₹{amount} (basis: {basis}; CA to verify)\n\n"
            "To the Auditor\n\n"
            "This is an editable starting template under SA 580. Management must customize, "
            "verify, date, and sign it. No representation below is asserted as fact by the system.\n\n"
            "Please address, as applicable and after management verification:\n"
            "1. Responsibility for preparation of the financial statements under the applicable framework.\n"
            "2. Completeness of accounting records, minutes, contracts, and information provided.\n"
            "3. Recognition, measurement, presentation, and disclosure of transactions and balances.\n"
            "4. Related parties, transactions, and related disclosures.\n"
            "5. Known or suspected fraud, allegations, and non-compliance communicated to the auditor.\n"
            "6. Litigation, claims, commitments, guarantees, and contingent liabilities.\n"
            "7. Subsequent events and going-concern information relevant to the applicable framework.\n"
            "8. Uncorrected misstatements, if any, and management's view on their effect.\n"
            "9. Other representations specifically requested by the auditor for this engagement.\n\n"
            "Management must replace each item with an accurate representation or a clearly stated exception.\n\n"
            "For and on behalf of management: [name, designation, date, signature]")


@router.post("/{engagement_id}/management-rep-letters/generate", response_model=schemas.ManagementRepLetterOut)
def generate_mrl(engagement_id: str, request: Request,
                 actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                 tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    previous = tenant.query(models.ManagementRepLetter).filter(
        models.ManagementRepLetter.engagement_id == engagement.id
    ).order_by(models.ManagementRepLetter.version.desc()).first()
    row = models.ManagementRepLetter(engagement_id=engagement.id,
        version=(previous.version + 1) if previous else 1, template_version="SA580-India-v1",
        draft_content=_mrl_template(engagement), final_content=None,
        status=models.ManagementRepLetterStatus.DRAFT, prepared_by=actor.id)
    tenant.add(row); tenant.flush()
    _audit(tenant, request, actor, AuditAction.MANAGEMENT_REP_LETTER_DRAFTED,
           "ManagementRepLetter", row.id, {"engagement_id": engagement.id, "version": row.version,
           "template_version": row.template_version, "draft_content": row.draft_content})
    tenant.commit(); tenant.refresh(row)
    return row


@router.get("/{engagement_id}/management-rep-letters", response_model=list[schemas.ManagementRepLetterOut])
def list_mrl(engagement_id: str, _: models.User = Depends(require_role(Role.ASSOCIATE)),
             tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    return tenant.query(models.ManagementRepLetter).filter(
        models.ManagementRepLetter.engagement_id == engagement_id
    ).order_by(models.ManagementRepLetter.version.desc()).all()


@router.put("/{engagement_id}/management-rep-letters/{letter_id}/edit", response_model=schemas.ManagementRepLetterOut)
def edit_mrl(engagement_id: str, letter_id: str, payload: schemas.ManagementRepLetterEditIn,
             request: Request, actor: models.User = Depends(require_role(Role.ASSOCIATE)),
             tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    row = tenant.query(models.ManagementRepLetter).filter_by(id=letter_id, engagement_id=engagement_id).one_or_none()
    if row is None: raise HTTPException(404, "Management representation letter not found")
    if row.prepared_by != actor.id or row.status != models.ManagementRepLetterStatus.DRAFT:
        raise HTTPException(409, "Only its preparer can edit an MRL draft awaiting review.")
    row.final_content = payload.final_content.strip()
    _audit(tenant, request, actor, AuditAction.MANAGEMENT_REP_LETTER_EDITED,
           "ManagementRepLetter", row.id, {"draft_content": row.draft_content, "associate_edited_content": row.final_content})
    tenant.commit(); tenant.refresh(row); return row


@router.post("/{engagement_id}/management-rep-letters/{letter_id}/review", response_model=schemas.ManagementRepLetterOut)
def review_mrl(engagement_id: str, letter_id: str, payload: schemas.WorkingPaperReviewIn,
               request: Request, actor: models.User = Depends(require_role(Role.MANAGER)),
               tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    row = tenant.query(models.ManagementRepLetter).filter_by(id=letter_id, engagement_id=engagement_id).one_or_none()
    if row is None: raise HTTPException(404, "Management representation letter not found")
    if actor.id == row.prepared_by: raise HTTPException(409, "Self-review is not permitted.")
    if row.status != models.ManagementRepLetterStatus.DRAFT: raise HTTPException(409, "Only a draft MRL can be reviewed.")
    if payload.decision == "approve":
        if not row.final_content or not row.final_content.strip(): raise HTTPException(409, "Associate-edited MRL content is required.")
        row.status = models.ManagementRepLetterStatus.MANAGER_REVIEWED; row.reviewed_by = actor.id; row.reviewed_at = _now()
    else:
        row.status = models.ManagementRepLetterStatus.DRAFT; row.reviewed_by = None; row.reviewed_at = None
    row.review_notes = payload.notes or ("Returned for rework." if payload.decision == "rework" else None)
    _audit(tenant, request, actor, AuditAction.MANAGEMENT_REP_LETTER_REVIEWED, "ManagementRepLetter", row.id,
           {"decision": payload.decision, "notes": row.review_notes, "status": row.status.value})
    tenant.commit(); tenant.refresh(row); return row


@router.post("/{engagement_id}/management-rep-letters/{letter_id}/approve", response_model=schemas.ManagementRepLetterOut)
def approve_mrl(engagement_id: str, letter_id: str, request: Request,
                actor: models.User = Depends(require_role(Role.PARTNER)), tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id); _assigned_partner(engagement, actor)
    row = tenant.query(models.ManagementRepLetter).filter_by(id=letter_id, engagement_id=engagement_id).one_or_none()
    if row is None: raise HTTPException(404, "Management representation letter not found")
    if row.status != models.ManagementRepLetterStatus.MANAGER_REVIEWED or not row.reviewed_by:
        raise HTTPException(409, "Manager review is required before partner approval.")
    if actor.id in (row.prepared_by, row.reviewed_by): raise HTTPException(409, "Self-approval is not permitted.")
    row.status = models.ManagementRepLetterStatus.PARTNER_APPROVED; row.approved_by = actor.id; row.approved_at = _now()
    _audit(tenant, request, actor, AuditAction.MANAGEMENT_REP_LETTER_APPROVED, "ManagementRepLetter", row.id,
           {"status": row.status.value, "final_content": row.final_content})
    tenant.commit(); tenant.refresh(row); return row


@router.post("/{engagement_id}/management-rep-letters/{letter_id}/send", response_model=schemas.ManagementRepLetterOut)
def mark_mrl_sent(engagement_id: str, letter_id: str, request: Request,
                  actor: models.User = Depends(require_role(Role.PARTNER)), tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id); _assigned_partner(engagement, actor)
    row = tenant.query(models.ManagementRepLetter).filter_by(id=letter_id, engagement_id=engagement_id).one_or_none()
    if row is None: raise HTTPException(404, "Management representation letter not found")
    if row.status != models.ManagementRepLetterStatus.PARTNER_APPROVED: raise HTTPException(409, "Partner approval is required before a CA explicitly records sending.")
    row.status = models.ManagementRepLetterStatus.SENT; row.sent_to_client_at = _now()
    _audit(tenant, request, actor, AuditAction.MANAGEMENT_REP_LETTER_SENT, "ManagementRepLetter", row.id,
           {"sent_to_client_at": row.sent_to_client_at.isoformat(), "method": "explicit-user-action"})
    tenant.commit(); tenant.refresh(row); return row


@router.post("/{engagement_id}/management-rep-letters/{letter_id}/receive", response_model=schemas.ManagementRepLetterOut)
def mark_mrl_received(engagement_id: str, letter_id: str, request: Request,
                      actor: models.User = Depends(require_role(Role.ASSOCIATE)), tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    row = tenant.query(models.ManagementRepLetter).filter_by(id=letter_id, engagement_id=engagement_id).one_or_none()
    if row is None: raise HTTPException(404, "Management representation letter not found")
    if row.status != models.ManagementRepLetterStatus.SENT: raise HTTPException(409, "Only a sent letter can be marked as signed and received.")
    row.status = models.ManagementRepLetterStatus.RECEIVED; row.received_signed_at = _now()
    _audit(tenant, request, actor, AuditAction.MANAGEMENT_REP_LETTER_RECEIVED, "ManagementRepLetter", row.id,
           {"received_signed_at": row.received_signed_at.isoformat()})
    tenant.commit(); tenant.refresh(row); return row


def _working_paper_snapshot(tenant, engagement):
    snapshot = build_source_snapshot(tenant, engagement)
    flags = tenant.query(models.RiskFlag).filter_by(engagement_id=engagement.id).all()
    ratios = tenant.query(models.AuditRatioAnalysis).filter_by(engagement_id=engagement.id).order_by(models.AuditRatioAnalysis.version.desc()).first()
    confirmations = tenant.query(models.BankConfirmation).filter_by(engagement_id=engagement.id).all()
    snapshot["related_party_flags"] = [{"id": f.id, "note": f.system_note, "status": f.status.value}
        for f in flags if f.flag_type == models.RiskFlagType.RELATED_PARTY]
    snapshot["risk_flags"] = [{"id": f.id, "type": f.flag_type.value, "severity": f.severity.value,
        "status": f.status.value, "note": f.system_note} for f in flags]
    snapshot["ratio_analysis"] = ({"id": ratios.id, "version": ratios.version, "status": ratios.status.value,
        "ratio_results": ratios.ratio_results} if ratios else None)
    snapshot["confirmation_tracking"] = [{"id": b.id, "status": b.letter_status.value,
        "masked_account": b.account_number_last4} for b in confirmations]
    procedures = tenant.query(models.AuditProcedure).filter_by(engagement_id=engagement.id).all()
    snapshot["audit_procedures"] = [{"id": p.id, "sa_reference": p.sa_reference,
        "procedure_name": p.procedure_name, "status": p.status.value,
        "evidence_file_paths": p.evidence_file_paths,
        "conclusion_notes": p.conclusion_notes if p.status == models.AuditProcedureStatus.PARTNER_APPROVED else None,
        "reviewed_by": p.reviewed_by, "approved_by": p.approved_by} for p in procedures]
    return snapshot


@router.post("/{engagement_id}/procedures", response_model=schemas.AuditProcedureOut)
def create_audit_procedure(engagement_id: str, payload: schemas.AuditProcedureCreateIn,
                           request: Request, actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                           tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    if engagement.status not in (models.AuditEngagementStatus.FIELDWORK, models.AuditEngagementStatus.COMPLETION):
        raise HTTPException(409, "Procedures may be added during Fieldwork or Completion.")
    row = models.AuditProcedure(engagement_id=engagement.id, sa_reference=payload.sa_reference.strip(),
        procedure_name=payload.procedure_name.strip(), status=models.AuditProcedureStatus.PLANNED,
        performed_by=actor.id, evidence_file_paths=[])
    tenant.add(row); tenant.flush()
    _audit(tenant, request, actor, AuditAction.AUDIT_PROCEDURE_PLANNED, "AuditProcedure", row.id,
        {"engagement_id": engagement.id, "sa_reference": row.sa_reference,
         "procedure_name": row.procedure_name, "status": row.status.value})
    tenant.commit(); tenant.refresh(row); return row


@router.get("/{engagement_id}/procedures", response_model=list[schemas.AuditProcedureOut])
def list_audit_procedures(engagement_id: str, _: models.User = Depends(require_role(Role.ASSOCIATE)),
                          tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    return tenant.query(models.AuditProcedure).filter_by(engagement_id=engagement_id).order_by(models.AuditProcedure.sa_reference).all()


@router.post("/{engagement_id}/procedures/{procedure_id}/perform", response_model=schemas.AuditProcedureOut)
def perform_audit_procedure(engagement_id: str, procedure_id: str, payload: schemas.AuditProcedurePerformIn,
                            request: Request, actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                            tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    row = tenant.query(models.AuditProcedure).filter_by(id=procedure_id, engagement_id=engagement.id).one_or_none()
    if row is None: raise HTTPException(404, "Audit procedure not found")
    if row.performed_by != actor.id: raise HTTPException(403, "Only the assigned preparer may record procedure work.")
    if row.status not in (models.AuditProcedureStatus.PLANNED, models.AuditProcedureStatus.REWORK):
        raise HTTPException(409, "Only a planned or returned procedure can be performed or revised.")
    row.status = models.AuditProcedureStatus.PERFORMED; row.performed_at = _now()
    row.evidence_file_paths = payload.evidence_file_paths; row.conclusion_notes = payload.conclusion_notes.strip()
    _audit(tenant, request, actor, AuditAction.AUDIT_PROCEDURE_PERFORMED, "AuditProcedure", row.id,
        {"engagement_id": engagement.id, "evidence_file_paths": row.evidence_file_paths,
         "preparer_notes": row.conclusion_notes, "status": row.status.value})
    tenant.commit(); tenant.refresh(row); return row


@router.post("/{engagement_id}/procedures/{procedure_id}/review", response_model=schemas.AuditProcedureOut)
def review_audit_procedure(engagement_id: str, procedure_id: str, payload: schemas.WorkingPaperReviewIn,
                           request: Request, actor: models.User = Depends(require_role(Role.MANAGER)),
                           tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    row = tenant.query(models.AuditProcedure).filter_by(id=procedure_id, engagement_id=engagement_id).one_or_none()
    if row is None: raise HTTPException(404, "Audit procedure not found")
    if actor.id == row.performed_by: raise HTTPException(409, "Self-review is not permitted.")
    if row.status != models.AuditProcedureStatus.PERFORMED: raise HTTPException(409, "Only performed work can be reviewed.")
    if payload.decision == "approve": row.status = models.AuditProcedureStatus.MANAGER_REVIEWED; row.reviewed_by = actor.id; row.reviewed_at = _now()
    else: row.status = models.AuditProcedureStatus.REWORK; row.reviewed_by = None; row.reviewed_at = None
    row.review_notes = payload.notes or ("Returned for rework." if payload.decision == "rework" else None)
    _audit(tenant, request, actor, AuditAction.AUDIT_PROCEDURE_REVIEWED, "AuditProcedure", row.id,
        {"decision": payload.decision, "notes": row.review_notes, "status": row.status.value})
    tenant.commit(); tenant.refresh(row); return row


@router.post("/{engagement_id}/procedures/{procedure_id}/approve", response_model=schemas.AuditProcedureOut)
def approve_audit_procedure(engagement_id: str, procedure_id: str, request: Request,
                            actor: models.User = Depends(require_role(Role.PARTNER)), tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id); _assigned_partner(engagement, actor)
    row = tenant.query(models.AuditProcedure).filter_by(id=procedure_id, engagement_id=engagement.id).one_or_none()
    if row is None: raise HTTPException(404, "Audit procedure not found")
    if row.status != models.AuditProcedureStatus.MANAGER_REVIEWED or not row.reviewed_by:
        raise HTTPException(409, "Manager review is required before partner approval.")
    if actor.id in (row.performed_by, row.reviewed_by): raise HTTPException(409, "Self-approval is not permitted.")
    row.status = models.AuditProcedureStatus.PARTNER_APPROVED; row.approved_by = actor.id; row.approved_at = _now()
    _audit(tenant, request, actor, AuditAction.AUDIT_PROCEDURE_APPROVED, "AuditProcedure", row.id,
        {"status": row.status.value, "conclusion_notes": row.conclusion_notes})
    tenant.commit(); tenant.refresh(row); return row


@router.post("/{engagement_id}/working-papers/compile", response_model=schemas.WorkingPaperOut)
def compile_working_paper(engagement_id: str, request: Request,
                          actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                          tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    snapshot = _working_paper_snapshot(tenant, engagement)
    prior = tenant.query(models.WorkingPaper).filter_by(engagement_id=engagement.id).order_by(models.WorkingPaper.version.desc()).first()
    refs = []
    for row in snapshot["risk_flags"]: refs.append({"type": "RiskFlag", "id": row["id"]})
    for row in snapshot["latest_ageing_schedules"]: refs.append({"type": "AgeingSchedule", "id": row["id"]})
    if snapshot["ratio_analysis"]: refs.append({"type": "AuditRatioAnalysis", "id": snapshot["ratio_analysis"]["id"]})
    for row in snapshot["audit_procedures"]: refs.append({"type": "AuditProcedure", "id": row["id"]})
    caro = tenant.query(models.CAROClauseResponse).filter_by(engagement_id=engagement.id).all()
    refs.extend({"type": "CAROClauseResponse", "id": r.id} for r in caro)
    content = ("SYSTEM COMPILED WORKING PAPER — DRAFT FOR CA REVIEW\n\n"
        "This compilation organizes recorded data and references. It is not audit evidence evaluation, a conclusion, or a signed working paper. Verify source documents and applicable SAs.\n\n"
        "## Risk and anomaly data (SA 315 planning aid; CA to evaluate)\n" + json.dumps(snapshot["risk_flags"], ensure_ascii=False, indent=2, default=str) +
        "\n\n## Statutory filing data (CA to reconcile to source returns)\n" + json.dumps(snapshot["statutory_filing_records"], ensure_ascii=False, indent=2, default=str) +
        "\n\n## Ageing schedules (SA 500 evidence organization aid)\n" + json.dumps(snapshot["latest_ageing_schedules"], ensure_ascii=False, indent=2, default=str) +
        "\n\n## Ratio analytics (SA 520 analytical procedure aid; not a conclusion)\n" + json.dumps(snapshot["ratio_analysis"], ensure_ascii=False, indent=2, default=str) +
        "\n\n## Bank confirmations (SA 505 tracking)\n" + json.dumps(snapshot["confirmation_tracking"], ensure_ascii=False, indent=2, default=str) +
        "\n\n## Audit procedures and approval state\n" + json.dumps(snapshot["audit_procedures"], ensure_ascii=False, indent=2, default=str) +
        "\n\n## Related-party flags (SA 550 review required)\n" + json.dumps(snapshot["related_party_flags"], ensure_ascii=False, indent=2, default=str) +
        "\n\n## CARO response status (narratives included only after partner finalization)\n" + json.dumps([{"id": r.id, "clause": r.clause_number, "status": r.status.value,
            "narrative": r.final_narrative if r.status == models.CAROResponseStatus.FINALIZED else None} for r in caro],
            ensure_ascii=False, indent=2, default=str))
    row = models.WorkingPaper(engagement_id=engagement.id, version=(prior.version + 1) if prior else 1,
        title=f"Audit file compilation FY {engagement.financial_year}",
        sa_reference="SA 315; SA 500; SA 505; SA 520; SA 550; SA 580",
        content=content, cross_references=refs, source_snapshot=snapshot,
        status=models.WorkingPaperStatus.DRAFT, prepared_by=actor.id)
    tenant.add(row); tenant.flush()
    _audit(tenant, request, actor, AuditAction.WORKING_PAPER_COMPILED, "WorkingPaper", row.id,
           {"engagement_id": engagement.id, "version": row.version, "source_snapshot": snapshot,
            "content": content, "cross_references": refs})
    tenant.commit(); tenant.refresh(row); return row


@router.get("/{engagement_id}/working-papers", response_model=list[schemas.WorkingPaperOut])
def list_working_papers(engagement_id: str, _: models.User = Depends(require_role(Role.ASSOCIATE)), tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    return tenant.query(models.WorkingPaper).filter_by(engagement_id=engagement_id).order_by(models.WorkingPaper.version.desc()).all()


@router.post("/{engagement_id}/working-papers/{paper_id}/review", response_model=schemas.WorkingPaperOut)
def review_working_paper(engagement_id: str, paper_id: str, payload: schemas.WorkingPaperReviewIn,
                         request: Request, actor: models.User = Depends(require_role(Role.MANAGER)), tenant: TenantSession = Depends(get_tenant)):
    _engagement(tenant, engagement_id)
    row = tenant.query(models.WorkingPaper).filter_by(id=paper_id, engagement_id=engagement_id).one_or_none()
    if row is None: raise HTTPException(404, "Working paper not found")
    if actor.id == row.prepared_by: raise HTTPException(409, "Self-review is not permitted.")
    if row.status != models.WorkingPaperStatus.DRAFT: raise HTTPException(409, "Only a draft working paper can be reviewed.")
    if payload.decision == "approve": row.status = models.WorkingPaperStatus.MANAGER_REVIEWED; row.reviewed_by = actor.id; row.reviewed_at = _now()
    else: row.status = models.WorkingPaperStatus.DRAFT; row.reviewed_by = None; row.reviewed_at = None
    row.review_notes = payload.notes or ("Returned for rework." if payload.decision == "rework" else None)
    _audit(tenant, request, actor, AuditAction.WORKING_PAPER_REVIEWED, "WorkingPaper", row.id,
           {"decision": payload.decision, "notes": row.review_notes, "status": row.status.value})
    tenant.commit(); tenant.refresh(row); return row


@router.post("/{engagement_id}/working-papers/{paper_id}/approve", response_model=schemas.WorkingPaperOut)
def approve_working_paper(engagement_id: str, paper_id: str, request: Request,
                          actor: models.User = Depends(require_role(Role.PARTNER)), tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id); _assigned_partner(engagement, actor)
    row = tenant.query(models.WorkingPaper).filter_by(id=paper_id, engagement_id=engagement_id).one_or_none()
    if row is None: raise HTTPException(404, "Working paper not found")
    if row.status != models.WorkingPaperStatus.MANAGER_REVIEWED or not row.reviewed_by: raise HTTPException(409, "Manager review is required before partner approval.")
    if actor.id in (row.prepared_by, row.reviewed_by): raise HTTPException(409, "Self-approval is not permitted.")
    row.status = models.WorkingPaperStatus.PARTNER_APPROVED; row.approved_by = actor.id; row.approved_at = _now()
    _audit(tenant, request, actor, AuditAction.WORKING_PAPER_APPROVED, "WorkingPaper", row.id,
           {"status": row.status.value, "source_snapshot": row.source_snapshot})
    tenant.commit(); tenant.refresh(row); return row


def _export_ready(tenant, engagement):
    assessment = tenant.get(models.CAROApplicabilityAssessment, engagement.caro_assessment_id) if engagement.caro_assessment_id else None
    if (assessment is None or not assessment.confirmed_by
            or assessment.confirmed_by != engagement.engagement_partner_id
            or engagement.caro_confirmed_by != engagement.engagement_partner_id):
        raise HTTPException(409, "Assigned-partner confirmation of CARO applicability is required before export.")
    if engagement.caro_applicable:
        for number in assessment.applicable_clause_numbers or []:
            response = tenant.query(models.CAROClauseResponse).filter_by(
                engagement_id=engagement.id, clause_number=int(number)).order_by(models.CAROClauseResponse.version.desc()).first()
            if response is None or response.status != models.CAROResponseStatus.FINALIZED or not response.final_narrative:
                raise HTTPException(409, f"CARO clause {number} is not partner-finalized; export is blocked.")
    paper = tenant.query(models.WorkingPaper).filter_by(engagement_id=engagement.id,
        status=models.WorkingPaperStatus.PARTNER_APPROVED).order_by(models.WorkingPaper.version.desc()).first()
    if paper is None: raise HTTPException(409, "A manager-reviewed, partner-approved working paper is required before export.")
    pending_procedures = tenant.query(models.AuditProcedure).filter_by(engagement_id=engagement.id).filter(
        models.AuditProcedure.status != models.AuditProcedureStatus.PARTNER_APPROVED).count()
    if pending_procedures:
        raise HTTPException(409, "Every recorded audit procedure must complete manager review and partner approval before export.")
    letter = tenant.query(models.ManagementRepLetter).filter_by(engagement_id=engagement.id).order_by(models.ManagementRepLetter.version.desc()).first()
    if letter is None:
        raise HTTPException(409, "A management representation letter is required before export.")
    if (letter.status not in (models.ManagementRepLetterStatus.PARTNER_APPROVED,
            models.ManagementRepLetterStatus.SENT, models.ManagementRepLetterStatus.RECEIVED) or not letter.final_content):
        raise HTTPException(409, "The latest management representation letter must be partner-approved before export.")
    return paper, letter


@router.post("/{engagement_id}/move-to-reporting")
def move_to_reporting(engagement_id: str, request: Request,
                      actor: models.User = Depends(require_role(Role.PARTNER)), tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id); _assigned_partner(engagement, actor)
    if engagement.status != models.AuditEngagementStatus.COMPLETION:
        raise HTTPException(409, "Only a Completion-stage engagement can move to Reporting.")
    _export_ready(tenant, engagement)
    letter = tenant.query(models.ManagementRepLetter).filter_by(engagement_id=engagement.id).order_by(models.ManagementRepLetter.version.desc()).first()
    if letter is None or letter.status not in (models.ManagementRepLetterStatus.PARTNER_APPROVED,
            models.ManagementRepLetterStatus.SENT, models.ManagementRepLetterStatus.RECEIVED):
        raise HTTPException(409, "A partner-approved management representation letter is required before Reporting.")
    engagement.status = models.AuditEngagementStatus.REPORTING
    _audit(tenant, request, actor, AuditAction.AUDIT_ENGAGEMENT_MOVED_TO_REPORTING,
           "AuditEngagement", engagement.id, {"status": "REPORTING", "partner_id": actor.id})
    tenant.commit()
    return {"id": engagement.id, "status": engagement.status.value}


@router.post("/{engagement_id}/move-to-completion")
def move_to_completion(engagement_id: str, request: Request,
                       actor: models.User = Depends(require_role(Role.PARTNER)), tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id); _assigned_partner(engagement, actor)
    if engagement.status != models.AuditEngagementStatus.FIELDWORK:
        raise HTTPException(409, "Only a Fieldwork-stage engagement can move to Completion.")
    engagement.status = models.AuditEngagementStatus.COMPLETION
    _audit(tenant, request, actor, AuditAction.AUDIT_ENGAGEMENT_MOVED_TO_COMPLETION,
           "AuditEngagement", engagement.id, {"status": "COMPLETION", "partner_id": actor.id,
           "human_action": True})
    tenant.commit()
    return {"id": engagement.id, "status": engagement.status.value}


@router.get("/{engagement_id}/audit-package/export")
def export_audit_package(engagement_id: str, request: Request,
                         actor: models.User = Depends(require_role(Role.PARTNER)), tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id); _assigned_partner(engagement, actor)
    paper, letter = _export_ready(tenant, engagement)
    finalized = tenant.query(models.CAROClauseResponse).filter_by(
        engagement_id=engagement.id, status=models.CAROResponseStatus.FINALIZED).order_by(models.CAROClauseResponse.clause_number).all()
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("README.txt", "APPROVED AUDIT FILE PACKAGE\nPrepared from partner-approved records. This package does not replace the auditor's signed report or professional judgment.\n")
        package.writestr("working-paper.md", paper.content)
        package.writestr("working-paper-metadata.json", json.dumps({"version": paper.version, "sa_references": paper.sa_reference,
            "approved_by": paper.approved_by, "approved_at": paper.approved_at.isoformat() if paper.approved_at else None,
            "cross_references": paper.cross_references}, ensure_ascii=False, indent=2, default=str))
        package.writestr("caro-final-responses.json", json.dumps([{"clause_number": r.clause_number,
            "clause_definition_id": r.clause_definition_id, "narrative": r.final_narrative,
            "finalized_by": r.finalized_by, "finalized_at": r.finalized_at.isoformat() if r.finalized_at else None}
            for r in finalized], ensure_ascii=False, indent=2))
        if letter:
            package.writestr("management-representation-letter-final.txt", letter.final_content)
    output.seek(0)
    _audit(tenant, request, actor, AuditAction.AUDIT_PACKAGE_EXPORTED, "AuditEngagement", engagement.id,
           {"working_paper_id": paper.id, "caro_response_ids": [r.id for r in finalized],
            "management_rep_letter_id": letter.id if letter else None, "included_drafts": False})
    tenant.commit()
    return StreamingResponse(output, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="audit-{engagement.financial_year}-{engagement.id}.zip"'})


@router.post("/{engagement_id}/close")
def close_engagement(engagement_id: str, request: Request,
                     actor: models.User = Depends(require_role(Role.PARTNER)), tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id); _assigned_partner(engagement, actor)
    _export_ready(tenant, engagement)
    if engagement.status != models.AuditEngagementStatus.REPORTING: raise HTTPException(409, "Engagement must be in Reporting before closure.")
    engagement.status = models.AuditEngagementStatus.CLOSED
    _audit(tenant, request, actor, AuditAction.AUDIT_ENGAGEMENT_CLOSED, "AuditEngagement", engagement.id,
           {"status": "CLOSED", "partner_id": actor.id})
    tenant.commit()
    return {"id": engagement.id, "status": engagement.status.value}
