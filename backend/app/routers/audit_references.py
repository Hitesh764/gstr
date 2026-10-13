"""Read-only endpoints for the versioned SA and CARO reference catalogs."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app import models, schemas
from app.deps import Role, get_tenant, require_role
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/audit-references", tags=["audit-references"])


@router.get("/standards", response_model=list[schemas.AuditStandardReferenceOut])
def list_standards(
    sa_number: Optional[str] = Query(None, max_length=20),
    as_of: Optional[date] = None,
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    query = tenant.query_reference(models.AuditStandardReference)
    if sa_number:
        query = query.filter(models.AuditStandardReference.sa_number == sa_number.strip().upper())
    if as_of:
        query = query.filter(
            models.AuditStandardReference.effective_from <= as_of,
            (models.AuditStandardReference.effective_to.is_(None)
             | (models.AuditStandardReference.effective_to >= as_of)),
        )
    return query.order_by(
        models.AuditStandardReference.sa_number.asc(),
        models.AuditStandardReference.version.desc(),
    ).all()


@router.get("/caro-clauses", response_model=list[schemas.CAROClauseDefinitionOut])
def list_caro_clauses(
    clause_number: Optional[int] = Query(None, ge=1, le=21),
    as_of: Optional[date] = None,
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    query = tenant.query_reference(models.CAROClauseDefinition)
    if clause_number is not None:
        query = query.filter(models.CAROClauseDefinition.clause_number == clause_number)
    if as_of:
        query = query.filter(
            models.CAROClauseDefinition.effective_from <= as_of,
            (models.CAROClauseDefinition.effective_to.is_(None)
             | (models.CAROClauseDefinition.effective_to >= as_of)),
        )
    return query.order_by(
        models.CAROClauseDefinition.clause_number.asc(),
        models.CAROClauseDefinition.version.desc(),
    ).all()
