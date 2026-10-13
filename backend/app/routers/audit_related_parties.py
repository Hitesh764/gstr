"""Client related-party master and exact-match audit review flags."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app import audit, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.services.related_party_matching import normalize_party_name, recheck_engagement
from app.tenancy import TenantSession

router = APIRouter(tags=["audit-related-parties"])


def _validate_dates(start, end):
    if start and end and end < start:
        raise HTTPException(422, "effective_to must be on or after effective_from.")


def _scan_client(tenant, client_id, actor):
    results = []
    engagements = tenant.query(models.AuditEngagement).filter(
        models.AuditEngagement.client_id == client_id,
    ).all()
    for engagement in engagements:
        results.append(recheck_engagement(tenant, engagement, actor))
    return results


@router.get("/api/clients/{client_id}/related-parties", response_model=list[schemas.RelatedPartyOut])
def list_related_parties(client_id: str,
                         _: models.User = Depends(require_role(Role.ASSOCIATE)),
                         tenant: TenantSession = Depends(get_tenant)):
    if tenant.get(models.Client, client_id) is None:
        raise HTTPException(404, "Client not found")
    return tenant.query(models.RelatedParty).filter(
        models.RelatedParty.client_id == client_id,
    ).order_by(models.RelatedParty.name).all()


@router.post("/api/clients/{client_id}/related-parties", response_model=schemas.RelatedPartyOut,
             status_code=status.HTTP_201_CREATED)
def create_related_party(client_id: str, payload: schemas.RelatedPartyCreate,
                         request: Request,
                         actor: models.User = Depends(require_role(Role.MANAGER)),
                         tenant: TenantSession = Depends(get_tenant)):
    if tenant.get(models.Client, client_id) is None:
        raise HTTPException(404, "Client not found")
    _validate_dates(payload.effective_from, payload.effective_to)
    normalized = normalize_party_name(payload.name)
    if not normalized:
        raise HTTPException(422, "Name must contain at least one letter or number.")
    party = models.RelatedParty(
        client_id=client_id, name=payload.name.strip(), normalized_name=normalized,
        aliases=payload.aliases, pan=payload.pan, relationship_nature=payload.relationship_nature.strip(),
        effective_from=payload.effective_from, effective_to=payload.effective_to,
        is_active=True, created_by=actor.id, updated_by=actor.id,
    )
    tenant.add(party)
    try:
        tenant.flush()
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                     action=AuditAction.RELATED_PARTY_CREATED, entity_type="RelatedParty",
                     entity_id=party.id, metadata={"client_id": client_id, "name": party.name,
                     "aliases": party.aliases, "relationship_nature": party.relationship_nature},
                     ip_address=client_ip(request), user_agent=user_agent(request))
        _scan_client(tenant, client_id, actor)
        tenant.commit()
    except IntegrityError as exc:
        tenant.rollback()
        raise HTTPException(409, "A related-party master entry with that name already exists.") from exc
    tenant.refresh(party)
    return party


@router.patch("/api/clients/{client_id}/related-parties/{party_id}",
              response_model=schemas.RelatedPartyOut)
def update_related_party(client_id: str, party_id: str, payload: schemas.RelatedPartyUpdate,
                         request: Request,
                         actor: models.User = Depends(require_role(Role.MANAGER)),
                         tenant: TenantSession = Depends(get_tenant)):
    party = tenant.query(models.RelatedParty).filter(
        models.RelatedParty.id == party_id, models.RelatedParty.client_id == client_id,
    ).one_or_none()
    if party is None:
        raise HTTPException(404, "Related-party entry not found")
    changes = payload.model_dump(exclude_unset=True)
    old = {key: getattr(party, key) for key in changes}
    for key, value in changes.items():
        if key == "name":
            value = value.strip()
            normalized = normalize_party_name(value)
            if not normalized:
                raise HTTPException(422, "Name must contain at least one letter or number.")
            party.normalized_name = normalized
        if key == "relationship_nature" and value is not None:
            value = value.strip()
        setattr(party, key, value)
    _validate_dates(party.effective_from, party.effective_to)
    party.updated_by = actor.id
    try:
        tenant.flush()
        action = AuditAction.RELATED_PARTY_DEACTIVATED if changes.get("is_active") is False else AuditAction.RELATED_PARTY_UPDATED
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                     action=action, entity_type="RelatedParty", entity_id=party.id,
                     metadata={"client_id": client_id, "before": old,
                               "after": {key: getattr(party, key) for key in changes}},
                     ip_address=client_ip(request), user_agent=user_agent(request))
        _scan_client(tenant, client_id, actor)
        tenant.commit()
    except IntegrityError as exc:
        tenant.rollback()
        raise HTTPException(409, "A related-party master entry with that name already exists.") from exc
    tenant.refresh(party)
    return party


@router.delete("/api/clients/{client_id}/related-parties/{party_id}",
               response_model=schemas.RelatedPartyOut)
def deactivate_related_party(client_id: str, party_id: str, request: Request,
                             actor: models.User = Depends(require_role(Role.MANAGER)),
                             tenant: TenantSession = Depends(get_tenant)):
    party = tenant.query(models.RelatedParty).filter(
        models.RelatedParty.id == party_id, models.RelatedParty.client_id == client_id,
    ).one_or_none()
    if party is None:
        raise HTTPException(404, "Related-party entry not found")
    party.is_active = False
    party.updated_by = actor.id
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
                 action=AuditAction.RELATED_PARTY_DEACTIVATED, entity_type="RelatedParty",
                 entity_id=party.id, metadata={"client_id": client_id, "name": party.name,
                 "is_active": False}, ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(party)
    return party


@router.post("/api/audit-engagements/{engagement_id}/related-party-matches/recheck",
             response_model=schemas.RelatedPartyMatchResultOut)
def recheck_related_party_matches(engagement_id: str,
                                  actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                                  tenant: TenantSession = Depends(get_tenant)):
    engagement = tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")
    result = recheck_engagement(tenant, engagement, actor)
    tenant.commit()
    return result
