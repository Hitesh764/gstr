"""
Live GST portal access: connecting a GSTIN, and fetching GSTR-2B on demand.

The flow a person goes through:

    1. request OTP   we ask the provider to have the GST portal send an OTP to
                     the taxpayer's registered contact. We never see or relay it.
    2. verify OTP    the person types what arrived; we exchange it for an access
                     token, encrypt it, and record when the consent ends, as
                     reported by the provider.
    3. fetch         from then on, GSTR-2B can be pulled on demand here or by the
                     scheduled job, until the consent window closes.

Guard rails, each of which exists because the alternative is a real problem:

  * OTP requests are throttled per GSTIN. Each one sends a text message to the
    client's phone and counts against the GST portal, so an unthrottled button
    is a way to harass a client and to get an account locked.
  * A limited number of wrong OTP entries per request, then a new OTP is needed.
  * The access token is never returned by any endpoint and never logged.
  * Roles: Associate can connect and fetch; only Manager and above can disconnect.

Every route takes a TenantSession, so another firm's GSTIN is a 404.
"""
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from app import audit, models
from app.audit import AuditAction
from app.config import settings
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.gsp import (
    GSPConfigurationError, GSPError, GSPProvider, GSPRateLimitedError, GSPUnavailableError,
    GSPValidationError, GSTINNotFoundError, InvalidOTPError, OTPRequestError,
    get_gsp_provider,
)
from app.services import consent as consent_svc
from app.services.consent import ConsentState
from app.services.gstr2b_sync import SyncStatus, previous_period, sync_gstr2b
from app.tenancy import TenantSession

router = APIRouter(prefix="/api/gstins", tags=["gsp"])

OTP_RE = re.compile(r"^\d{4,8}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._@\-]{2,100}$")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def get_provider() -> GSPProvider:
    """Dependency, so tests can substitute a scripted provider."""
    try:
        return get_gsp_provider()
    except GSPConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _http(exc: GSPError) -> HTTPException:
    """Provider failures as HTTP errors. Messages are ours, written for people."""
    if isinstance(exc, GSPRateLimitedError):
        code = 429
    elif isinstance(exc, (GSPUnavailableError, GSPConfigurationError)):
        code = 503
    elif isinstance(exc, (GSPValidationError, OTPRequestError, GSTINNotFoundError, InvalidOTPError)):
        code = 422
    else:
        code = 502
    return HTTPException(status_code=code, detail=str(exc))


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class OTPRequestIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=100,
                          description="The taxpayer's GST portal username")

    @field_validator("username")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = v.strip()
        if not USERNAME_RE.match(v):
            raise ValueError("A GST portal username contains letters, digits and . _ @ - only.")
        return v


class OTPRequestOut(BaseModel):
    attempt_id: str
    message: str
    expires_at: datetime
    provider: str


class OTPVerifyIn(BaseModel):
    attempt_id: str
    otp: str = Field(..., min_length=4, max_length=8)


class FetchAttemptOut(BaseModel):
    id: str
    period: str
    trigger: str
    status: str
    attempt_number: int
    error_code: Optional[str] = None
    message: Optional[str] = None
    next_retry_at: Optional[datetime] = None
    invoice_count: Optional[int] = None
    credit_note_count: Optional[int] = None
    created_at: datetime


class GspStatusOut(BaseModel):
    gstin_id: str
    gstin: str
    active_provider: str
    connected_provider: Optional[str] = None
    provider_is_mock: bool
    state: str                                   # NONE | ACTIVE | EXPIRING | EXPIRED
    portal_username: Optional[str] = None
    granted_at: Optional[datetime] = None
    session_expires_at: Optional[datetime] = None
    token_expires_at: Optional[datetime] = None
    last_refreshed_at: Optional[datetime] = None
    seconds_remaining: Optional[int] = None
    window_note: Optional[str] = None
    renew_before_fetch_window: Optional[str] = None
    next_fetch_period: str
    recent_attempts: list[FetchAttemptOut] = Field(default_factory=list)


class ConsentVerifyOut(BaseModel):
    status: GspStatusOut
    window_note: Optional[str] = None


class FetchOut(BaseModel):
    status: str                                  # SUCCESS | FAILED | SKIPPED | RETRY
    message: str
    error_code: Optional[str] = None
    fetch_id: Optional[str] = None
    run_id: Optional[str] = None
    invoice_count: Optional[int] = None
    credit_note_count: Optional[int] = None
    reconciled: bool = False
    warnings: list[str] = Field(default_factory=list)


class GstinInfoOut(BaseModel):
    gstin: str
    legal_name: Optional[str] = None
    trade_name: Optional[str] = None
    status: str
    is_active: bool
    state_name: Optional[str] = None
    warning: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window_note(remaining: Optional[timedelta], total: Optional[timedelta]) -> Optional[str]:
    """Explain a short consent window, derived from what the provider reported.

    The point is practical: with a window shorter than the gap between fetch
    days, hands-off monthly fetching cannot work, and the fix is on the GST
    portal, not here. Says nothing when the window is comfortably long.
    """
    if total is None:
        return None
    if total < timedelta(days=15):
        return (f"This consent lasts {consent_svc.humanize(total)}. Automatic monthly fetching "
                f"needs access to be live on the 14th, so it will only work if this is renewed "
                f"shortly before then. To avoid that, ask the client to set 'Manage API Access' "
                f"on the GST portal to the longest available duration (up to 30 days).")
    return None


def _status(tenant: TenantSession, g: models.ClientGSTIN, provider: GSPProvider) -> GspStatusOut:
    now = datetime.utcnow()
    state = consent_svc.consent_state(g, now)
    remaining = ((g.consent_expires_at - now) if g.consent_expires_at and state
                 in (ConsentState.ACTIVE, ConsentState.EXPIRING) else None)
    total = ((g.consent_expires_at - g.consent_granted_at)
             if g.consent_expires_at and g.consent_granted_at else None)
    renew = consent_svc.needs_renewal_before_window(g, now)

    attempts = (
        tenant.query(models.GSTR2BFetchAttempt)
        .filter(models.GSTR2BFetchAttempt.client_gstin_id == g.id)
        .order_by(models.GSTR2BFetchAttempt.created_at.desc())
        .limit(5).all()
    )
    return GspStatusOut(
        gstin_id=g.id, gstin=g.gstin, active_provider=provider.name,
        connected_provider=g.gsp_provider, provider_is_mock=provider.name == "mock",
        state=state.value, portal_username=g.gst_portal_username,
        granted_at=g.consent_granted_at, session_expires_at=g.consent_expires_at,
        token_expires_at=g.gsp_token_expires_at, last_refreshed_at=g.gsp_last_refreshed_at,
        seconds_remaining=int(remaining.total_seconds()) if remaining else None,
        window_note=_window_note(remaining, total),
        renew_before_fetch_window=renew.isoformat() if renew else None,
        next_fetch_period=previous_period(now),
        recent_attempts=[FetchAttemptOut(
            id=a.id, period=a.period,
            trigger=a.trigger.value if hasattr(a.trigger, "value") else a.trigger,
            status=a.status.value if hasattr(a.status, "value") else a.status,
            attempt_number=a.attempt_number, error_code=a.error_code, message=a.message,
            next_retry_at=a.next_retry_at, invoice_count=a.invoice_count,
            credit_note_count=a.credit_note_count, created_at=a.created_at,
        ) for a in attempts],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/{gstin_id}/gsp", response_model=GspStatusOut)
def gsp_status(
    gstin_id: str,
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
    provider: GSPProvider = Depends(get_provider),
):
    g = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    return _status(tenant, g, provider)


@router.post("/{gstin_id}/gsp/verify-gstin", response_model=GstinInfoOut)
def verify_gstin(
    gstin_id: str, request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
    provider: GSPProvider = Depends(get_provider),
):
    """Look the GSTIN up on the public register and record what it says."""
    g = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    try:
        info = provider.verify_gstin(g.gstin)
    except GSPError as exc:
        raise _http(exc) from exc

    g.legal_name = info.legal_name or g.legal_name
    g.trade_name = info.trade_name or g.trade_name
    g.state_name = info.state_name or g.state_name
    g.verified_at = datetime.utcnow()
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.GSP_GSTIN_VERIFIED, entity_type="ClientGSTIN", entity_id=g.id,
        metadata={"gstin": g.gstin, "status": info.status, "provider": provider.name},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    warning = None if info.is_active else (
        f"This registration is {info.status}. Returns may not be available, and "
        f"input tax credit from a {info.status.lower()} taxpayer needs care.")
    return GstinInfoOut(gstin=g.gstin, legal_name=info.legal_name, trade_name=info.trade_name,
                        status=info.status, is_active=info.is_active,
                        state_name=info.state_name, warning=warning)


@router.post("/{gstin_id}/gsp/consent/request", response_model=OTPRequestOut)
def request_otp(
    gstin_id: str, payload: OTPRequestIn, request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
    provider: GSPProvider = Depends(get_provider),
):
    g = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    now = datetime.utcnow()

    recent = (
        tenant.query(models.GSPConsentAttempt)
        .filter(models.GSPConsentAttempt.client_gstin_id == g.id,
                models.GSPConsentAttempt.requested_at >= now - timedelta(hours=1))
        .count()
    )
    if recent >= settings.gsp_otp_max_requests_per_hour:
        raise HTTPException(
            status_code=429,
            detail=(f"Too many OTP requests for this GSTIN in the last hour "
                    f"(limit {settings.gsp_otp_max_requests_per_hour}). Each one sends a text "
                    f"message to the client's phone. Wait a while before trying again."),
        )

    try:
        result = provider.initiate_otp_consent(g.gstin, payload.username)
    except GSPError as exc:
        raise _http(exc) from exc

    # Only one OTP request is live at a time: a new one supersedes the old.
    for old in (
        tenant.query(models.GSPConsentAttempt)
        .filter(models.GSPConsentAttempt.client_gstin_id == g.id,
                models.GSPConsentAttempt.status == models.ConsentAttemptStatus.PENDING)
        .all()
    ):
        old.status = models.ConsentAttemptStatus.EXPIRED
        old.completed_at = now
        old.failure_code = "SUPERSEDED"

    attempt = models.GSPConsentAttempt(
        client_gstin_id=g.id, provider=provider.name, username=payload.username,
        status=models.ConsentAttemptStatus.PENDING, requested_by=actor.id,
        requested_at=now,
        expires_at=now + timedelta(minutes=settings.gsp_otp_ttl_minutes),
        provider_transaction_id=result.transaction_id,
    )
    tenant.add(attempt)
    tenant.flush()
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.GSP_CONSENT_REQUESTED, entity_type="ClientGSTIN", entity_id=g.id,
        metadata={"gstin": g.gstin, "username": payload.username, "provider": provider.name},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    return OTPRequestOut(attempt_id=attempt.id, message=result.message,
                         expires_at=attempt.expires_at, provider=provider.name)


@router.post("/{gstin_id}/gsp/consent/verify", response_model=ConsentVerifyOut)
def verify_otp(
    gstin_id: str, payload: OTPVerifyIn, request: Request,
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
    provider: GSPProvider = Depends(get_provider),
):
    g = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    attempt = tenant.get(models.GSPConsentAttempt, payload.attempt_id)
    if attempt is None or attempt.client_gstin_id != g.id:
        raise HTTPException(status_code=404, detail="That OTP request was not found.")

    if attempt.status != models.ConsentAttemptStatus.PENDING:
        raise HTTPException(status_code=409,
                            detail="That OTP request is closed. Request a new OTP.")

    now = datetime.utcnow()
    if now >= attempt.expires_at:
        attempt.status = models.ConsentAttemptStatus.EXPIRED
        attempt.completed_at = now
        attempt.failure_code = "OTP_WINDOW_PASSED"
        tenant.commit()
        raise HTTPException(status_code=409,
                            detail="That OTP has expired. Request a new one.")

    otp = payload.otp.strip()
    if not OTP_RE.match(otp):
        # Rejected without calling the provider or spending an attempt.
        raise HTTPException(status_code=422, detail="An OTP is 4 to 8 digits.")

    attempt.verify_attempts += 1
    try:
        grant = provider.verify_otp_consent(g.gstin, attempt.username, otp)
    except InvalidOTPError as exc:
        left = settings.gsp_otp_max_verify_attempts - attempt.verify_attempts
        if left <= 0:
            attempt.status = models.ConsentAttemptStatus.FAILED
            attempt.completed_at = now
            attempt.failure_code = "TOO_MANY_WRONG_OTPS"
            tenant.commit()
            raise HTTPException(
                status_code=429,
                detail="Too many incorrect OTPs. Request a new OTP to try again.") from exc
        tenant.commit()
        raise HTTPException(
            status_code=422,
            detail=f"{exc} You have {left} attempt{'s' if left != 1 else ''} left.") from exc
    except GSPError as exc:
        if exc.retryable:
            # A provider fault is not the person's mistake, so it costs no attempt.
            attempt.verify_attempts -= 1
        else:
            attempt.status = models.ConsentAttemptStatus.FAILED
            attempt.completed_at = now
            attempt.failure_code = exc.code
        tenant.commit()
        raise _http(exc) from exc

    was_live = consent_svc.is_live(g, now)
    consent_svc.store_grant(g, grant, provider=provider.name, username=attempt.username,
                            first_grant=not was_live, now=now)
    attempt.status = models.ConsentAttemptStatus.VERIFIED
    attempt.completed_at = now
    attempt.provider_transaction_id = grant.transaction_id or attempt.provider_transaction_id

    # DPDP Act: a durable record of what was consented to, by whom, and until when.
    tenant.add(models.ConsentLog(
        client_id=g.client_id, client_gstin_id=g.id,
        consent_type=models.ConsentType.GSP_GSTR_ACCESS,
        purpose=("Read this GSTIN's GSTR-2B (and related returns) from the GST portal through "
                 f"{provider.name}, to reconcile input tax credit for the client."),
        granted_at=now, expires_at=grant.session_expires_at,
        granted_via="GSP_OTP", recorded_by=actor.id,
        evidence_reference=f"gsp_attempt:{attempt.id}",
    ))
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id, action=AuditAction.CONSENT_GRANTED,
        entity_type="ClientGSTIN", entity_id=g.id,
        metadata={"gstin": g.gstin, "provider": provider.name,
                  "session_expires_at": grant.session_expires_at.isoformat(),
                  "token_expires_at": grant.token_expires_at.isoformat()},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()

    total = grant.session_expires_at - now
    return ConsentVerifyOut(status=_status(tenant, g, provider),
                            window_note=_window_note(total, total))


@router.delete("/{gstin_id}/gsp/consent", response_model=GspStatusOut)
def disconnect(
    gstin_id: str, request: Request,
    actor: models.User = Depends(require_role(Role.MANAGER)),
    tenant: TenantSession = Depends(get_tenant),
    provider: GSPProvider = Depends(get_provider),
):
    """Stop using live access for this GSTIN and discard the stored credential.

    This ends OUR use of it. The GST portal session itself runs until it
    expires; the taxpayer can end it sooner under Manage API Access on the portal.
    """
    g = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    now = datetime.utcnow()
    consent_svc.revoke(g)
    for log in (
        tenant.query(models.ConsentLog)
        .filter(models.ConsentLog.client_gstin_id == g.id,
                models.ConsentLog.consent_type == models.ConsentType.GSP_GSTR_ACCESS,
                models.ConsentLog.withdrawn_at.is_(None))
        .all()
    ):
        log.withdrawn_at = now
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id, action=AuditAction.CONSENT_WITHDRAWN,
        entity_type="ClientGSTIN", entity_id=g.id, metadata={"gstin": g.gstin},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    return _status(tenant, g, provider)


@router.post("/{gstin_id}/gsp/fetch", response_model=FetchOut)
def fetch_now(
    gstin_id: str,
    period: str = Query(..., description="YYYY-MM"),
    actor: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
    provider: GSPProvider = Depends(get_provider),
):
    """Fetch GSTR-2B now, instead of waiting for the schedule.

    Runs the same code as the scheduled job but reports the outcome to the
    person waiting, rather than retrying invisibly in the background.
    """
    g = tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    if not PERIOD_RE.match(period or ""):
        raise HTTPException(status_code=422,
                            detail="Period must look like 2026-03 (year, then month).")
    if period > datetime.utcnow().strftime("%Y-%m"):
        raise HTTPException(status_code=422,
                            detail="That period is in the future; GSTR-2B does not exist yet.")

    outcome = sync_gstr2b(
        tenant.db, g.id, period, trigger=models.FetchTrigger.MANUAL,
        attempt=1, max_attempts=1, provider=provider, actor_id=actor.id, force=True,
    )
    return FetchOut(
        status=outcome.status.value, message=outcome.message, error_code=outcome.error_code,
        fetch_id=outcome.fetch_id, run_id=outcome.run_id, invoice_count=outcome.invoice_count,
        credit_note_count=outcome.credit_note_count, reconciled=outcome.reconciled,
        warnings=outcome.warnings,
    )


@router.get("/{gstin_id}/gsp/attempts", response_model=list[FetchAttemptOut])
def fetch_history(
    gstin_id: str,
    period: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    tenant.get_or_404(models.ClientGSTIN, gstin_id, "GSTIN not found")
    q = tenant.query(models.GSTR2BFetchAttempt).filter(
        models.GSTR2BFetchAttempt.client_gstin_id == gstin_id)
    if period:
        q = q.filter(models.GSTR2BFetchAttempt.period == period)
    rows = q.order_by(models.GSTR2BFetchAttempt.created_at.desc()).limit(limit).all()
    return [FetchAttemptOut(
        id=a.id, period=a.period,
        trigger=a.trigger.value if hasattr(a.trigger, "value") else a.trigger,
        status=a.status.value if hasattr(a.status, "value") else a.status,
        attempt_number=a.attempt_number, error_code=a.error_code, message=a.message,
        next_retry_at=a.next_retry_at, invoice_count=a.invoice_count,
        credit_note_count=a.credit_note_count, created_at=a.created_at,
    ) for a in rows]
