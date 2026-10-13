"""Versioned CARO drafts with separated associate, manager, and partner actions."""
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.services.caro_responses import build_source_snapshot
from app.services.audit_analysis import AuditAnalysisError, AuditAnalysisService
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/audit-engagements", tags=["audit-caro-responses"])


def _engagement_work(tenant, engagement_id):
    engagement = tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    if engagement.status not in (models.AuditEngagementStatus.FIELDWORK,
                                  models.AuditEngagementStatus.COMPLETION):
        raise HTTPException(409, "CARO responses can be prepared or reviewed during Fieldwork or Completion.")
    return engagement


def _assigned_partner(engagement, actor):
    if actor.id != engagement.engagement_partner_id or actor.role != models.UserRole.PARTNER:
        raise HTTPException(403, "Only the assigned engagement partner can finalize CARO responses.")


def _response(tenant, engagement_id, response_id):
    row = tenant.query(models.CAROClauseResponse).filter(
        models.CAROClauseResponse.engagement_id == engagement_id,
        models.CAROClauseResponse.id == response_id,
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "CARO clause response not found")
    return row


@router.post("/{engagement_id}/caro-responses/generate",
             response_model=list[schemas.CAROClauseResponseOut])
def generate_caro_drafts(engagement_id: str, request: Request,
                         actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                         tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement_work(tenant, engagement_id)
    assessment = tenant.get(models.CAROApplicabilityAssessment, engagement.caro_assessment_id)
    if (assessment is None or assessment.confirmed_by != engagement.engagement_partner_id
            or engagement.caro_confirmed_by != engagement.engagement_partner_id):
        raise HTTPException(409, "Partner-confirmed CARO applicability is required before drafting responses.")
    if not assessment.confirmed_applicable:
        raise HTTPException(409, "Partner-confirmed CARO applicability is Not Applicable; no clause responses will be drafted.")
    clause_numbers = sorted(set(int(n) for n in assessment.applicable_clause_numbers))
    if not clause_numbers:
        raise HTTPException(409, "The confirmed applicability assessment contains no in-scope clauses.")
    effective = date(int(engagement.financial_year[:4]), 4, 1)
    definitions = tenant.query_reference(models.CAROClauseDefinition).filter(
        models.CAROClauseDefinition.clause_number.in_(clause_numbers),
        models.CAROClauseDefinition.effective_from <= effective,
        (models.CAROClauseDefinition.effective_to.is_(None)
         | (models.CAROClauseDefinition.effective_to >= effective)),
    ).order_by(models.CAROClauseDefinition.clause_number,
               models.CAROClauseDefinition.version.desc()).all()
    latest = {}
    for definition in definitions:
        latest.setdefault(definition.clause_number, definition)
    missing = sorted(set(clause_numbers) - set(latest))
    if missing:
        raise HTTPException(409, f"Versioned CARO clause text is missing for clauses: {missing}.")

    snapshot = build_source_snapshot(tenant, engagement)
    analysis = AuditAnalysisService()
    created = []
    for number in clause_numbers:
        definition = latest[number]
        prior = tenant.query(models.CAROClauseResponse).filter(
            models.CAROClauseResponse.engagement_id == engagement.id,
            models.CAROClauseResponse.clause_number == number,
        ).order_by(models.CAROClauseResponse.version.desc()).all()
        try:
            generated = analysis.draft_caro(clause_number=number, title=definition.title,
                                            clause_text=definition.clause_text,
                                            source_snapshot=snapshot)
        except AuditAnalysisError as exc:
            attempts = exc.attempts
            for attempt_no, attempt in enumerate(attempts, 1):
                record = models.AuditAIRecord(
                    engagement_id=engagement.id, entity_type="CAROClauseResponseRejected",
                    entity_id=engagement.id, attempt_number=attempt_no,
                    provider=attempt.provider, model=attempt.model,
                    exact_prompt=attempt.exact_prompt, exact_response=attempt.exact_response,
                    response_sha256=attempt.response_sha256, accepted=False,
                    generated_by=actor.id)
                tenant.add(record)
                tenant.flush()
                audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                    action=AuditAction.AUDIT_AI_SUGGESTION_RECORDED,
                    entity_type="CAROClauseResponseRejected", entity_id=engagement.id,
                    metadata={"engagement_id": engagement.id, "clause_number": number,
                              "ai_record_id": record.id, "attempt": attempt_no,
                              "accepted": False, "exact_prompt": attempt.exact_prompt,
                              "exact_response": attempt.exact_response,
                              "response_sha256": attempt.response_sha256},
                    ip_address=client_ip(request), user_agent=user_agent(request))
            tenant.commit()
            raise HTTPException(503, "Draft generation failed safety checks; rejected attempts were recorded and no CARO draft was saved.") from exc
        for old in prior:
            if old.status != models.CAROResponseStatus.FINALIZED:
                old.status = models.CAROResponseStatus.SUPERSEDED
        row = models.CAROClauseResponse(
            engagement_id=engagement.id, clause_definition_id=definition.id,
            clause_number=number, version=(prior[0].version + 1) if prior else 1,
            applicability="Applicable", applicability_reasoning=assessment.reasoning,
            draft_narrative=generated.narrative,
            final_narrative=None, source_snapshot=snapshot,
            status=models.CAROResponseStatus.DRAFT, prepared_by=actor.id,
        )
        tenant.add(row)
        tenant.flush()
        for attempt_no, attempt in enumerate(generated.attempts, 1):
            record = models.AuditAIRecord(
                engagement_id=engagement.id, entity_type="CAROClauseResponse",
                entity_id=row.id, attempt_number=attempt_no, provider=attempt.provider,
                model=attempt.model, exact_prompt=attempt.exact_prompt,
                exact_response=attempt.exact_response, response_sha256=attempt.response_sha256,
                accepted=attempt.accepted, generated_by=actor.id)
            tenant.add(record)
            tenant.flush()
            audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                action=AuditAction.AUDIT_AI_SUGGESTION_RECORDED,
                entity_type="CAROClauseResponse", entity_id=row.id,
                metadata={"engagement_id": engagement.id, "clause_number": number,
                          "ai_record_id": record.id, "attempt": attempt_no,
                          "accepted": attempt.accepted, "exact_prompt": attempt.exact_prompt,
                          "exact_response": attempt.exact_response,
                          "response_sha256": attempt.response_sha256},
                ip_address=client_ip(request), user_agent=user_agent(request))
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                     action=AuditAction.CARO_RESPONSE_DRAFT_GENERATED,
                     entity_type="CAROClauseResponse", entity_id=row.id,
                     metadata={"engagement_id": engagement.id, "clause_number": number,
                               "version": row.version, "clause_definition_id": definition.id,
                               "draft_narrative": row.draft_narrative,
                               "source_snapshot": snapshot,
                               "superseded_response_ids": [old.id for old in prior
                                   if old.status == models.CAROResponseStatus.SUPERSEDED]},
                     ip_address=client_ip(request), user_agent=user_agent(request))
        created.append(row)
    tenant.commit()
    for row in created:
        tenant.refresh(row)
    return created


@router.get("/{engagement_id}/caro-responses",
            response_model=list[schemas.CAROClauseResponseOut])
def list_caro_responses(engagement_id: str,
                        _: models.User = Depends(require_role(Role.ASSOCIATE)),
                        tenant: TenantSession = Depends(get_tenant)):
    tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    return tenant.query(models.CAROClauseResponse).filter(
        models.CAROClauseResponse.engagement_id == engagement_id,
    ).order_by(models.CAROClauseResponse.clause_number,
               models.CAROClauseResponse.version.desc()).all()


@router.put("/{engagement_id}/caro-responses/{response_id}/edit",
            response_model=schemas.CAROClauseResponseOut)
def edit_caro_response(engagement_id: str, response_id: str,
                       payload: schemas.CAROResponseEditIn, request: Request,
                       actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                       tenant: TenantSession = Depends(get_tenant)):
    _engagement_work(tenant, engagement_id)
    row = _response(tenant, engagement_id, response_id)
    if actor.id != row.prepared_by:
        raise HTTPException(403, "Only the preparing associate may edit this response before manager review.")
    if row.status != models.CAROResponseStatus.DRAFT:
        raise HTTPException(409, "Only a draft awaiting manager review can be edited.")
    row.final_narrative = payload.final_narrative.strip()
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                 action=AuditAction.CARO_RESPONSE_EDITED, entity_type="CAROClauseResponse",
                 entity_id=row.id, metadata={"engagement_id": engagement_id,
                 "clause_number": row.clause_number, "draft_narrative": row.draft_narrative,
                 "associate_authored_narrative": row.final_narrative},
                 ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row


@router.post("/{engagement_id}/caro-responses/{response_id}/review",
             response_model=schemas.CAROClauseResponseOut)
def review_caro_response(engagement_id: str, response_id: str,
                         payload: schemas.CAROResponseReviewIn, request: Request,
                         actor: models.User = Depends(require_role(Role.MANAGER)),
                         tenant: TenantSession = Depends(get_tenant)):
    _engagement_work(tenant, engagement_id)
    row = _response(tenant, engagement_id, response_id)
    if actor.id == row.prepared_by:
        raise HTTPException(409, "Self-review is not permitted.")
    if row.status != models.CAROResponseStatus.DRAFT:
        raise HTTPException(409, "Only a draft can be reviewed.")
    if payload.decision == "approve":
        if not row.final_narrative or not row.final_narrative.strip():
            raise HTTPException(409, "Associate-authored narrative is required before manager review.")
        row.status = models.CAROResponseStatus.MANAGER_REVIEWED
        row.reviewed_by = actor.id
        row.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.review_notes = payload.notes
    else:
        row.status = models.CAROResponseStatus.DRAFT
        row.review_notes = payload.notes or "Returned for rework."
        row.reviewed_by = None
        row.reviewed_at = None
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                 action=AuditAction.CARO_RESPONSE_MANAGER_REVIEWED,
                 entity_type="CAROClauseResponse", entity_id=row.id,
                 metadata={"engagement_id": engagement_id, "clause_number": row.clause_number,
                           "decision": payload.decision, "notes": row.review_notes,
                           "status": row.status.value},
                 ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row


@router.post("/{engagement_id}/caro-responses/{response_id}/finalize",
             response_model=schemas.CAROClauseResponseOut)
def finalize_caro_response(engagement_id: str, response_id: str,
                           payload: schemas.CAROResponseFinalizeIn, request: Request,
                           actor: models.User = Depends(require_role(Role.PARTNER)),
                           tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement_work(tenant, engagement_id)
    _assigned_partner(engagement, actor)
    row = _response(tenant, engagement_id, response_id)
    if row.status != models.CAROResponseStatus.MANAGER_REVIEWED or not row.reviewed_by:
        raise HTTPException(409, "Manager review is required before partner finalization.")
    if actor.id in (row.prepared_by, row.reviewed_by):
        raise HTTPException(409, "The partner finalizer must differ from the preparer and manager reviewer.")
    row.final_narrative = payload.final_narrative.strip()
    if not row.final_narrative:
        raise HTTPException(422, "Final CARO narrative cannot be empty.")
    row.status = models.CAROResponseStatus.FINALIZED
    row.finalized_by = actor.id
    row.finalized_at = datetime.now(timezone.utc).replace(tzinfo=None)
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                 action=AuditAction.CARO_RESPONSE_FINALIZED,
                 entity_type="CAROClauseResponse", entity_id=row.id,
                 metadata={"engagement_id": engagement_id, "clause_number": row.clause_number,
                           "clause_definition_id": row.clause_definition_id,
                           "system_draft": row.draft_narrative,
                           "final_narrative": row.final_narrative,
                           "prepared_by": row.prepared_by, "reviewed_by": row.reviewed_by,
                           "finalized_by": actor.id, "status": row.status.value},
                 ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row
