"""
Versioned storage of ingested data.

Shared by the manual upload endpoints and the scheduled GSP fetch, so that a
GSTR-2B arriving by either route is stored identically: as a new version, with
earlier versions kept, and the newest marked current.

Nothing here knows about HTTP or about who is asking. Callers pass a
TenantSession, so every read and write is scoped to one firm.
"""
from datetime import datetime
from typing import Optional

from app import models
from app.tenancy import TenantSession


def next_version(tenant: TenantSession, model, gstin_id: str, period: str) -> int:
    latest = (
        tenant.query(model)
        .filter(model.client_gstin_id == gstin_id, model.period == period)
        .order_by(model.version.desc())
        .first()
    )
    return (latest.version + 1) if latest else 1


def supersede_previous(tenant: TenantSession, model, gstin_id: str, period: str) -> None:
    """Mark earlier versions as no longer current. They are NOT deleted."""
    for row in (
        tenant.query(model)
        .filter(model.client_gstin_id == gstin_id,
                model.period == period,
                model.is_current.is_(True))
        .all()
    ):
        row.is_current = False


def current_gstr2b_fetch(tenant: TenantSession, gstin_id: str, period: str,
                         source: Optional[models.FetchSource] = None):
    q = (
        tenant.query(models.GSTR2BFetch)
        .filter(models.GSTR2BFetch.client_gstin_id == gstin_id,
                models.GSTR2BFetch.period == period,
                models.GSTR2BFetch.is_current.is_(True))
    )
    if source is not None:
        q = q.filter(models.GSTR2BFetch.source == source)
    return q.first()


def current_purchase_upload(tenant: TenantSession, gstin_id: str, period: str):
    return (
        tenant.query(models.PurchaseRegisterUpload)
        .filter(models.PurchaseRegisterUpload.client_gstin_id == gstin_id,
                models.PurchaseRegisterUpload.period == period,
                models.PurchaseRegisterUpload.is_current.is_(True))
        .first()
    )


def store_gstr2b(
    tenant: TenantSession,
    gstin_id: str,
    period: str,
    rows: list,
    meta: dict,
    source: models.FetchSource,
    actor_id: Optional[str],
    raw_payload: Optional[dict] = None,
    gsp_provider: Optional[str] = None,
) -> models.GSTR2BFetch:
    """Store a parsed GSTR-2B as a new current version."""
    version = next_version(tenant, models.GSTR2BFetch, gstin_id, period)
    supersede_previous(tenant, models.GSTR2BFetch, gstin_id, period)

    generated_on = None
    if meta.get("generated_on"):
        try:
            generated_on = datetime.fromisoformat(str(meta["generated_on"])).date()
        except (ValueError, TypeError):
            generated_on = None

    fetch = models.GSTR2BFetch(
        client_gstin_id=gstin_id,
        period=period,
        version=version,
        is_current=True,
        source=source,
        gsp_provider=gsp_provider,
        generated_on=generated_on,
        raw_payload=raw_payload,
        fetched_by=actor_id,
    )
    tenant.add(fetch)
    tenant.flush()
    for row in rows:
        tenant.add(models.GSTR2BEntry(fetch_id=fetch.id, **row))
    return fetch
