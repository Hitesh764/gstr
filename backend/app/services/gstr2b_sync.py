"""
Fetching GSTR-2B from the GST portal, and what follows from it.

This module is deliberately plain functions over a database session, with no
Celery in it. The background worker is a thin wrapper (app/worker/tasks.py),
which means every path here can be tested by calling a function, and the same
code serves both the nightly schedule and the "fetch now" button.

The rule the whole module is organised around: **no outcome is silent.**
Every run of `sync_gstr2b` ends in exactly one of four states, each written to
`gstr2b_fetch_attempts` in words a person can act on:

    SUCCESS   stored, reconciled if there is a purchase register, associate told
    RETRY     a transient fault (GSTN down, rate limited, return not generated
              yet); another attempt is scheduled with exponential backoff
    FAILED    final. Either the fault is not worth retrying (consent expired,
              GSTIN suspended, malformed response) or retries ran out. The
              associate is told the specific reason.
    SKIPPED   nothing to do (already fetched; never connected to live access)

Only the failures that deserve a retry are retried, and that decision comes
from the error's own `retryable` flag, never from parsing its message.

Nothing here logs or returns an access token.
"""
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, models
from app.audit import AuditAction
from app.config import settings
from app.gsp import (
    ConsentExpiredError, GSPConfigurationError, GSPError, GSPProvider,
    GSPRateLimitedError, get_gsp_provider,
)
from app.ingestion.gstr2b import parse_gstr2b_json
from app.notifications import NotificationType, notify_client_owner
from app.services import consent as consent_svc
from app.services.consent import ConsentState
from app.services.ingest import current_gstr2b_fetch, current_purchase_upload, store_gstr2b
from app.services.reconciliation_service import perform_reconciliation
from app.tenancy import TenantSession

logger = logging.getLogger("gstr2b.sync")

BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600


class SyncStatus(str, Enum):
    SUCCESS = "SUCCESS"
    RETRY = "RETRY"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class SyncOutcome:
    status: SyncStatus
    message: str
    error_code: Optional[str] = None
    retry_in: Optional[int] = None          # seconds, when status is RETRY
    fetch_id: Optional[str] = None
    run_id: Optional[str] = None
    invoice_count: Optional[int] = None
    credit_note_count: Optional[int] = None
    reconciled: bool = False
    warnings: list = field(default_factory=list)


def backoff_seconds(attempt: int, *, floor: int = 0, rng=random) -> int:
    """Exponential backoff with jitter: 1, 2, 4, 8, 16, 32 minutes, capped at an hour.

    `attempt` is the attempt that just failed, counting from 1. Jitter of up to
    10% stops a hundred GSTINs that failed together from all retrying together,
    which is what turns a brief GSTN outage into a sustained one. `floor`
    honours a provider's Retry-After hint.
    """
    base = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)))
    return int(max(base, floor) * (1 + rng.random() * 0.1))


def previous_period(now: Optional[datetime] = None) -> str:
    """The period whose GSTR-2B is generated in the current month.

    GSTR-2B for March is generated on 14 April, so a run on any day in April
    fetches March. Uses Indian calendar dates, which is how GSTN counts.
    """
    today = consent_svc.to_ist(now or datetime.utcnow())
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return f"{year:04d}-{month:02d}"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _record(
    tenant: TenantSession, gstin: models.ClientGSTIN, period: str, *,
    trigger: models.FetchTrigger, status: models.FetchAttemptStatus, attempt: int,
    message: str, error_code: Optional[str] = None, txn: Optional[str] = None,
    next_retry_at: Optional[datetime] = None, fetch_id: Optional[str] = None,
    run_id: Optional[str] = None, invoices: Optional[int] = None,
    notes: Optional[int] = None,
) -> models.GSTR2BFetchAttempt:
    row = models.GSTR2BFetchAttempt(
        client_gstin_id=gstin.id, period=period, trigger=trigger, status=status,
        attempt_number=attempt, error_code=error_code, message=message[:2000],
        provider_transaction_id=txn, next_retry_at=next_retry_at,
        fetch_id=fetch_id, reconciliation_run_id=run_id,
        invoice_count=invoices, credit_note_count=notes,
    )
    tenant.add(row)
    return row


def _describe(gstin: models.ClientGSTIN, client: models.Client) -> str:
    return f"{client.name} ({gstin.gstin})"


def _connect_link(gstin: models.ClientGSTIN) -> str:
    return f"/?connect={gstin.id}"


# ---------------------------------------------------------------------------
# One GSTIN, one period
# ---------------------------------------------------------------------------

def sync_gstr2b(
    db: Session,
    gstin_id: str,
    period: str,
    *,
    trigger: models.FetchTrigger = models.FetchTrigger.SCHEDULED,
    attempt: int = 1,
    max_attempts: Optional[int] = None,
    provider: Optional[GSPProvider] = None,
    actor_id: Optional[str] = None,
    force: bool = False,
    now: Optional[datetime] = None,
    rng=random,
) -> SyncOutcome:
    """Fetch, store and reconcile one GSTIN's GSTR-2B. Commits.

    `max_attempts` bounds retries. A manual fetch passes 1 so that a transient
    fault is reported to the person waiting rather than retried invisibly.
    """
    now = now or datetime.utcnow()
    max_attempts = max_attempts or settings.gstr2b_fetch_max_attempts
    scheduled = trigger is models.FetchTrigger.SCHEDULED

    # The one place a firm is discovered rather than given: a background job has
    # no session, only a GSTIN id. Everything after this is tenant-scoped.
    gstin = db.get(models.ClientGSTIN, gstin_id)
    if gstin is None or gstin.deleted_at is not None:
        return SyncOutcome(SyncStatus.SKIPPED, "That GSTIN no longer exists.")
    tenant = TenantSession(db=db, firm_id=gstin.firm_id)

    client = tenant.get(models.Client, gstin.client_id)
    if client is None:
        return SyncOutcome(SyncStatus.SKIPPED, "The client no longer exists.")
    label = _describe(gstin, client)

    if scheduled and client.status != models.ClientStatus.ACTIVE:
        return SyncOutcome(SyncStatus.SKIPPED, f"{client.name} is not an active client.")

    if scheduled and not force:
        existing = current_gstr2b_fetch(tenant, gstin.id, period, models.FetchSource.API)
        if existing is not None:
            _record(tenant, gstin, period, trigger=trigger,
                    status=models.FetchAttemptStatus.SKIPPED, attempt=attempt,
                    message="GSTR-2B for this period was already fetched.",
                    fetch_id=existing.id)
            tenant.commit()
            return SyncOutcome(SyncStatus.SKIPPED, "Already fetched.", fetch_id=existing.id)

    # ---- consent gate: never call the provider without live consent --------
    state = consent_svc.consent_state(gstin, now)
    if state is ConsentState.NONE:
        if scheduled:
            return SyncOutcome(SyncStatus.SKIPPED,
                               "Not connected to live GST access; manual upload applies.")
        return _fail(tenant, gstin, client, period, trigger, attempt,
                     code="NOT_CONNECTED", notify_owner=False,
                     message="This GSTIN is not connected to the GST portal. "
                             "Connect it first, or upload the GSTR-2B file.")

    if state is ConsentState.EXPIRED:
        return _fail_expired(tenant, gstin, client, period, trigger, attempt, scheduled)

    # ---- provider ----------------------------------------------------------
    try:
        provider = provider or get_gsp_provider()
    except GSPError as exc:
        return _handle_error(tenant, gstin, client, period, trigger, attempt,
                             max_attempts, exc, scheduled, rng)

    # ---- token (refreshing it silently if it is about to die) ----------------
    try:
        token = consent_svc.ensure_fresh_token(gstin, provider, now)
        tenant.commit()      # persist a refreshed token even if the fetch below fails
    except ConsentExpiredError as exc:
        tenant.commit()      # ensure_fresh_token may have dropped a dead token
        return _fail_expired(tenant, gstin, client, period, trigger, attempt, scheduled,
                             reason=str(exc))
    except GSPError as exc:
        return _handle_error(tenant, gstin, client, period, trigger, attempt,
                             max_attempts, exc, scheduled, rng)

    # ---- fetch ----------------------------------------------------------------
    try:
        result = provider.fetch_gstr2b(gstin.gstin, period, token)
    except ConsentExpiredError as exc:
        consent_svc.drop_token(gstin, now)
        tenant.commit()
        return _fail_expired(tenant, gstin, client, period, trigger, attempt, scheduled,
                             reason=str(exc))
    except GSPError as exc:
        return _handle_error(tenant, gstin, client, period, trigger, attempt,
                             max_attempts, exc, scheduled, rng)
    except Exception as exc:  # a bug must be loud, not swallowed by the scheduler
        logger.exception("Unexpected error fetching GSTR-2B for GSTIN %s period %s", gstin.id, period)
        return _fail(tenant, gstin, client, period, trigger, attempt, code="UNEXPECTED",
                     message=f"An unexpected error occurred while fetching GSTR-2B "
                             f"({type(exc).__name__}). The team has been alerted.",
                     notify_owner=scheduled)

    # ---- store -----------------------------------------------------------------
    rows, parse_errors, meta = parse_gstr2b_json(json.dumps(result.document).encode("utf-8"))
    warnings = list(result.warnings) + [f"Skipped: {e}" for e in parse_errors]
    raw_payload = {
        "provider": provider.name, "transaction_id": result.transaction_id,
        "credit_notes": result.document.get("credit_notes", []),
        "warnings": warnings, "response": result.raw,
    }
    try:
        fetch = store_gstr2b(
            tenant, gstin.id, period, rows, meta, models.FetchSource.API, actor_id,
            raw_payload=raw_payload, gsp_provider=provider.name,
        )
        tenant.flush()
    except IntegrityError:
        # Another worker stored a version for this period a moment ago.
        tenant.rollback()
        return SyncOutcome(SyncStatus.SKIPPED, "Another worker fetched this period first.")

    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor_id, action=AuditAction.FETCH_GSTR2B,
        entity_type="GSTR2BFetch", entity_id=fetch.id,
        metadata={
            "gstin": gstin.gstin, "period": period, "source": "API",
            "provider": provider.name, "rows": len(rows), "version": fetch.version,
            "credit_notes": result.credit_note_count, "pages": result.pages,
            "trigger": trigger.value, "transaction_id": result.transaction_id,
        },
    )

    # ---- reconcile, but only against a purchase register that exists ---------
    run = None
    reconciled_note = ""
    if current_purchase_upload(tenant, gstin.id, period) is not None:
        actor = tenant.get(models.User, actor_id) if actor_id else None
        outcome = perform_reconciliation(tenant, gstin, period, actor=actor,
                                         trigger=trigger.value)
        run = outcome.run
        reconciled_note = (f"Reconciliation ran: {outcome.results} lines, "
                           f"{outcome.flagged} need review.")
    else:
        reconciled_note = ("No purchase register has been uploaded for this period yet, "
                           "so nothing was reconciled. Upload it to reconcile.")

    notes_line = ""
    if result.credit_note_count:
        notes_line = (f" {result.credit_note_count} credit/debit note(s) in the statement are "
                      f"stored but not yet reconciled.")

    message = (f"Fetched GSTR-2B for {period}: {len(rows)} invoices."
               f"{notes_line} {reconciled_note}").strip()
    _record(tenant, gstin, period, trigger=trigger,
            status=models.FetchAttemptStatus.SUCCESS, attempt=attempt, message=message,
            txn=result.transaction_id, fetch_id=fetch.id,
            run_id=run.id if run else None, invoices=len(rows),
            notes=result.credit_note_count)

    if scheduled:
        notify_client_owner(
            db, client=client, event_type=NotificationType.GSTR2B_FETCH_COMPLETED,
            title=f"GSTR-2B received for {client.name}",
            message=f"{label}, {period}. {message}",
            link=(f"/gstins/{gstin.id}/dashboard/{period}" if run
                  else f"/gstins/{gstin.id}/upload"),
            entity_type="GSTR2BFetch", entity_id=fetch.id,
        )
    tenant.commit()

    return SyncOutcome(
        SyncStatus.SUCCESS, message, fetch_id=fetch.id, run_id=run.id if run else None,
        invoice_count=len(rows), credit_note_count=result.credit_note_count,
        reconciled=run is not None, warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Failure paths. Each records, notifies (for scheduled runs) and returns.
# ---------------------------------------------------------------------------

def _fail(
    tenant: TenantSession, gstin: models.ClientGSTIN, client: models.Client, period: str,
    trigger: models.FetchTrigger, attempt: int, *, code: str, message: str,
    notify_owner: bool, txn: Optional[str] = None,
) -> SyncOutcome:
    _record(tenant, gstin, period, trigger=trigger, status=models.FetchAttemptStatus.FAILED,
            attempt=attempt, message=message, error_code=code, txn=txn)
    if notify_owner:
        notify_client_owner(
            tenant.db, client=client, event_type=NotificationType.GSTR2B_FETCH_FAILED,
            title=f"GSTR-2B fetch failed for {client.name}",
            message=f"{_describe(gstin, client)}, {period}: {message}",
            link=f"/gstins/{gstin.id}/upload",
            entity_type="GSTR2BFetchFailure", entity_id=f"{gstin.id}:{period}:{code}",
            once=False,
        )
    tenant.commit()
    return SyncOutcome(SyncStatus.FAILED, message, error_code=code)


def _fail_expired(
    tenant: TenantSession, gstin: models.ClientGSTIN, client: models.Client, period: str,
    trigger: models.FetchTrigger, attempt: int, scheduled: bool,
    reason: Optional[str] = None,
) -> SyncOutcome:
    """Consent is unusable. `reason` carries the specific cause when there is one
    (a provider mismatch, an unreadable credential); otherwise it is simply that
    the window ended."""
    ended = gstin.consent_expires_at
    when = f" on {ended:%d %b %Y}" if ended else ""
    detail = reason or (f"GST portal access for {client.name} ended{when}. "
                        f"Connect again with a new OTP to resume automatic fetching.")
    message = f"{detail} GSTR-2B for {period} was not fetched."
    _record(tenant, gstin, period, trigger=trigger, status=models.FetchAttemptStatus.FAILED,
            attempt=attempt, message=message, error_code="CONSENT_EXPIRED")
    if scheduled:
        notify_client_owner(
            tenant.db, client=client, event_type=NotificationType.GSP_CONSENT_EXPIRED,
            title=f"GST portal access expired for {client.name}",
            message=f"{_describe(gstin, client)}: {message}",
            link=_connect_link(gstin),
            entity_type="ClientGSTIN",
            # The expiry time is part of the identity, so this notifies once per
            # lapse, and a later, different lapse notifies again.
            entity_id=f"{gstin.id}:expired:{ended.isoformat() if ended else 'unknown'}",
            once=True,
        )
    tenant.commit()
    return SyncOutcome(SyncStatus.FAILED, message, error_code="CONSENT_EXPIRED")


def _handle_error(
    tenant: TenantSession, gstin: models.ClientGSTIN, client: models.Client, period: str,
    trigger: models.FetchTrigger, attempt: int, max_attempts: int, exc: GSPError,
    scheduled: bool, rng,
) -> SyncOutcome:
    reason = str(exc)
    txn = exc.transaction_id
    ref = f" (reference {txn})" if txn else ""

    if exc.retryable and attempt < max_attempts:
        floor = exc.retry_after if isinstance(exc, GSPRateLimitedError) and exc.retry_after else 0
        delay = backoff_seconds(attempt, floor=floor, rng=rng)
        message = (f"{reason} Attempt {attempt} of {max_attempts}; "
                   f"trying again in {max(1, delay // 60)} minute(s).")
        _record(tenant, gstin, period, trigger=trigger,
                status=models.FetchAttemptStatus.RETRYING, attempt=attempt, message=message,
                error_code=exc.code, txn=txn,
                next_retry_at=datetime.utcnow() + timedelta(seconds=delay))
        tenant.commit()
        return SyncOutcome(SyncStatus.RETRY, message, error_code=exc.code, retry_in=delay)

    if exc.retryable and max_attempts > 1:
        message = f"Gave up after {attempt} attempt(s). Last error: {reason}{ref}"
    else:
        # Non-retryable, or a manual fetch where the person is waiting on the answer.
        message = f"{reason}{ref}"
    return _fail(tenant, gstin, client, period, trigger, attempt, code=exc.code,
                 message=message, notify_owner=scheduled, txn=txn)


# ---------------------------------------------------------------------------
# Fan-out: the nightly job
# ---------------------------------------------------------------------------

@dataclass
class DispatchReport:
    period: str
    enqueued: int = 0
    already_fetched: int = 0
    not_connected: int = 0
    expired: int = 0
    inactive: int = 0


def _live_gstins(db: Session):
    """Every connectable GSTIN across every firm.

    This is the one deliberately cross-tenant read in the module: a scheduler
    serves all firms, so it has no single firm to scope to. It returns only ids
    and lets the per-GSTIN work run tenant-scoped, and it excludes deleted firms,
    clients and GSTINs so a soft-deleted account is never fetched for.
    """
    return (
        db.query(models.ClientGSTIN, models.Client)
        .join(models.Client, models.Client.id == models.ClientGSTIN.client_id)
        .join(models.Firm, models.Firm.id == models.ClientGSTIN.firm_id)
        .filter(models.ClientGSTIN.deleted_at.is_(None),
                models.Client.deleted_at.is_(None),
                models.Firm.deleted_at.is_(None))
        .all()
    )


def dispatch_due_fetches(
    db: Session, *, enqueue: Callable[[str, str], None],
    now: Optional[datetime] = None,
) -> DispatchReport:
    """Queue a fetch for every GSTIN that should have one today.

    `enqueue(gstin_id, period)` is injected so this is testable without a
    broker. GSTINs with expired consent are NOT queued (auto-fetch is blocked
    until renewed) but their associate is told, once per lapse. GSTINs never
    connected are skipped without comment: manual upload is their permanent path.
    """
    now = now or datetime.utcnow()
    period = previous_period(now)
    report = DispatchReport(period=period)

    for gstin, client in _live_gstins(db):
        if client.status != models.ClientStatus.ACTIVE:
            report.inactive += 1
            continue

        state = consent_svc.consent_state(gstin, now)
        if state is ConsentState.NONE:
            report.not_connected += 1
            continue

        if state is ConsentState.EXPIRED:
            report.expired += 1
            ended = gstin.consent_expires_at
            notify_client_owner(
                db, client=client, event_type=NotificationType.GSP_CONSENT_EXPIRED,
                title=f"GST portal access expired for {client.name}",
                message=(f"{_describe(gstin, client)}: access ended"
                         f"{f' on {ended:%d %b %Y}' if ended else ''}, so GSTR-2B for {period} "
                         f"will not be fetched automatically. Connect again with a new OTP."),
                link=_connect_link(gstin), entity_type="ClientGSTIN",
                entity_id=f"{gstin.id}:expired:{ended.isoformat() if ended else 'unknown'}",
                once=True,
            )
            continue

        tenant = TenantSession(db=db, firm_id=gstin.firm_id)
        if current_gstr2b_fetch(tenant, gstin.id, period, models.FetchSource.API) is not None:
            report.already_fetched += 1
            continue

        enqueue(gstin.id, period)
        report.enqueued += 1

    db.commit()
    return report


# ---------------------------------------------------------------------------
# Housekeeping jobs
# ---------------------------------------------------------------------------

@dataclass
class HealthReport:
    expired_notified: int = 0
    expiring_notified: int = 0
    window_notified: int = 0


def check_consent_health(db: Session, *, now: Optional[datetime] = None) -> HealthReport:
    """Warn people about consent before it becomes a missed fetch.

    Three situations, each notified once per cause:
      EXPIRED   the window ended
      EXPIRING  the window closes soon
      WINDOW    the window closes before the upcoming fetch window does, so the
                14th would find it dead unless someone renews first
    """
    now = now or datetime.utcnow()
    report = HealthReport()

    for gstin, client in _live_gstins(db):
        if client.status != models.ClientStatus.ACTIVE:
            continue
        state = consent_svc.consent_state(gstin, now)
        if state is ConsentState.NONE:
            continue

        ends = gstin.consent_expires_at
        stamp = ends.isoformat() if ends else "unknown"

        if state is ConsentState.EXPIRED:
            n = notify_client_owner(
                db, client=client, event_type=NotificationType.GSP_CONSENT_EXPIRED,
                title=f"GST portal access expired for {client.name}",
                message=(f"{_describe(gstin, client)}: access ended"
                         f"{f' on {ends:%d %b %Y}' if ends else ''}. Automatic GSTR-2B fetching "
                         f"is paused until you connect again with a new OTP."),
                link=_connect_link(gstin), entity_type="ClientGSTIN",
                entity_id=f"{gstin.id}:expired:{stamp}", once=True)
            report.expired_notified += n
            continue

        if state is ConsentState.EXPIRING:
            n = notify_client_owner(
                db, client=client, event_type=NotificationType.GSP_CONSENT_EXPIRING,
                title=f"GST portal access for {client.name} is expiring",
                message=(f"{_describe(gstin, client)}: access ends in "
                         f"{consent_svc.humanize(ends - now)}. Renew it to keep automatic "
                         f"GSTR-2B fetching running."),
                link=_connect_link(gstin), entity_type="ClientGSTIN",
                entity_id=f"{gstin.id}:expiring:{stamp}", once=True)
            report.expiring_notified += n
            continue

        window_start = consent_svc.needs_renewal_before_window(gstin, now)
        if window_start is not None:
            n = notify_client_owner(
                db, client=client, event_type=NotificationType.GSP_CONSENT_EXPIRING,
                title=f"Renew GST portal access for {client.name} before the 14th",
                message=(f"{_describe(gstin, client)}: access ends "
                         f"{f'on {ends:%d %b %Y}' if ends else 'soon'}, before the GSTR-2B "
                         f"fetch window that opens {window_start:%d %b}. Renew it now or "
                         f"that fetch will be skipped."),
                link=_connect_link(gstin), entity_type="ClientGSTIN",
                entity_id=f"{gstin.id}:window:{window_start.isoformat()}", once=True)
            report.window_notified += n

    db.commit()
    return report


def refresh_expiring_tokens(
    db: Session, *, provider: Optional[GSPProvider] = None, now: Optional[datetime] = None,
) -> int:
    """Renew tokens that will die soon, while the session behind them lives.

    Runs often, so the nightly fetch finds a fresh token instead of having to
    refresh at the moment of need. Returns how many were renewed. A transient
    provider fault is skipped and picked up on the next pass; an ended session
    drops the dead token so the health check reports it.
    """
    now = now or datetime.utcnow()
    try:
        provider = provider or get_gsp_provider()
    except GSPConfigurationError:
        return 0
    if not provider.supports_refresh:
        return 0

    renewed = 0
    for gstin, client in _live_gstins(db):
        if not consent_svc.is_live(gstin, now) or not consent_svc.has_token(gstin):
            continue
        if gstin.gsp_provider and gstin.gsp_provider != provider.name:
            continue
        if not consent_svc.token_needs_refresh(gstin, now):
            continue
        before = gstin.gsp_token_expires_at
        try:
            consent_svc.ensure_fresh_token(gstin, provider, now)
            # ensure_fresh_token serves the still-live token when a refresh hits a
            # transient fault, so returning normally does not mean anything was
            # renewed. Count only a token whose expiry actually moved.
            if gstin.gsp_token_expires_at != before:
                renewed += 1
        except ConsentExpiredError:
            logger.info("Consent for GSTIN %s ended; token dropped.", gstin.id)
        except GSPError as exc:
            logger.warning("Could not refresh token for GSTIN %s: %s", gstin.id, exc)
    db.commit()
    return renewed
