"""
Celery tasks. Deliberately thin: each opens a session, calls one function from
app/services/gstr2b_sync.py, and translates its outcome into Celery's
vocabulary. All decisions live in the service, where they are testable without
a broker.
"""
import logging
from dataclasses import asdict

from app.config import settings
from app.database import SessionLocal
from app.models import FetchTrigger
from app.services import gstr2b_sync as sync
from app.services import notices as notices_svc
from app.worker.celery_app import celery_app

logger = logging.getLogger("gstr2b.worker")

# Attempts are numbered from 1, retries from 0: six attempts allow five retries.
MAX_RETRIES = max(0, settings.gstr2b_fetch_max_attempts - 1)


@celery_app.task(name="gstr2b.ping", ignore_result=False)
def ping() -> str:
    """Round-trips the broker. `celery call gstr2b.ping` proves a worker is alive."""
    return "pong"


@celery_app.task(name="gstr2b.fetch_gstin", bind=True, max_retries=MAX_RETRIES)
def fetch_gstr2b_task(self, gstin_id: str, period: str) -> str:
    """Fetch one GSTIN's GSTR-2B, retrying transient failures with backoff.

    The service decides whether a failure deserves a retry and how long to wait.
    This task only carries out that decision, so exhausting the retries produces
    the same recorded, notified failure as any other final failure.
    """
    attempt = self.request.retries + 1
    with SessionLocal() as db:
        outcome = sync.sync_gstr2b(
            db, gstin_id, period, trigger=FetchTrigger.SCHEDULED,
            attempt=attempt, max_attempts=settings.gstr2b_fetch_max_attempts,
        )
    logger.info("fetch %s %s attempt %s -> %s", gstin_id, period, attempt, outcome.status.value)

    if outcome.status is sync.SyncStatus.RETRY:
        raise self.retry(countdown=outcome.retry_in, max_retries=MAX_RETRIES)
    return outcome.status.value


@celery_app.task(name="gstr2b.dispatch_due_fetches")
def dispatch_due_fetches_task() -> dict:
    """Queue a fetch for every GSTIN due one today."""
    with SessionLocal() as db:
        report = sync.dispatch_due_fetches(
            db, enqueue=lambda gid, period: fetch_gstr2b_task.delay(gid, period),
        )
    logger.info("dispatch: %s", report)
    return asdict(report)


@celery_app.task(name="gstr2b.refresh_tokens")
def refresh_tokens_task() -> int:
    with SessionLocal() as db:
        return sync.refresh_expiring_tokens(db)


@celery_app.task(name="gstr2b.consent_health")
def consent_health_task() -> dict:
    with SessionLocal() as db:
        return asdict(sync.check_consent_health(db))


@celery_app.task(name="notices.deadline_alerts")
def notice_deadline_alerts_task() -> dict:
    """Alert on open notices approaching, reaching or past their deadlines."""
    with SessionLocal() as db:
        report = notices_svc.send_deadline_alerts(db)
    logger.info("notice alerts: %s", report)
    return asdict(report)
