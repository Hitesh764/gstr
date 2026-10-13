"""
The Celery layer: schedule, retry wiring, and the fetch job end to end.

Celery's eager mode runs retries immediately and in-process, with the attempt
counter advancing exactly as it would in a real worker, which lets these tests
drive the REAL task and the REAL service through their retry sequence with only
the provider scripted. No broker is needed.
"""
from contextlib import contextmanager
from unittest import mock

import pytest

from app import models
from app.gsp.base import (
    GSPResponseError, GSPUnavailableError, ReturnNotAvailableError,
)
from app.notifications import NotificationType
from app.services import gstr2b_sync as sync
from app.worker import tasks
from app.worker.celery_app import build_beat_schedule, celery_app
from tests.gsp_helpers import PERIOD, FakeGSP, World


@pytest.fixture
def fake():
    return FakeGSP()


@pytest.fixture
def wired(SessionFactory, fake):
    """Point the real task at the test database and the scripted provider."""
    with mock.patch.object(tasks, "SessionLocal", SessionFactory), \
         mock.patch.object(sync, "get_gsp_provider", lambda: fake):
        yield


# ---------------------------------------------------------------------------
# Schedule and reliability settings
# ---------------------------------------------------------------------------

def test_the_fetch_runs_on_the_14th_15th_and_16th_at_six_ist():
    entry = build_beat_schedule()["gstr2b-dispatch-due-fetches"]
    cron = entry["schedule"]
    assert entry["task"] == "gstr2b.dispatch_due_fetches"
    assert cron.day_of_month == {14, 15, 16}
    assert cron.hour == {6} and cron.minute == {0}
    assert celery_app.conf.timezone == "Asia/Kolkata"


def test_housekeeping_jobs_are_scheduled():
    schedule = build_beat_schedule()
    assert schedule["gsp-refresh-tokens"]["schedule"].minute == {0, 30}
    assert schedule["gsp-consent-health"]["task"] == "gstr2b.consent_health"


def test_the_worker_survives_a_crash_without_losing_the_fetch():
    conf = celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1


def test_six_attempts_allow_five_retries():
    assert tasks.MAX_RETRIES == 5
    assert tasks.fetch_gstr2b_task.max_retries == 5


def test_every_task_is_registered():
    names = set(celery_app.tasks)
    assert {"gstr2b.ping", "gstr2b.fetch_gstin", "gstr2b.dispatch_due_fetches",
            "gstr2b.refresh_tokens", "gstr2b.consent_health"} <= names


# ---------------------------------------------------------------------------
# Retry wiring, with the service replaced
# ---------------------------------------------------------------------------

@contextmanager
def _session():
    yield object()


def test_the_task_reports_the_attempt_number_and_stops_on_success():
    seen = []

    def fake_sync(db, gstin_id, period, **kw):
        seen.append(kw["attempt"])
        if kw["attempt"] < 3:
            return sync.SyncOutcome(sync.SyncStatus.RETRY, "blip", retry_in=1)
        return sync.SyncOutcome(sync.SyncStatus.SUCCESS, "ok")

    with mock.patch.object(tasks.sync, "sync_gstr2b", fake_sync), \
         mock.patch.object(tasks, "SessionLocal", _session):
        result = tasks.fetch_gstr2b_task.apply(args=["g1", PERIOD])
    assert seen == [1, 2, 3] and result.result == "SUCCESS"


def test_the_task_stops_retrying_at_the_limit_even_if_the_service_keeps_asking():
    seen = []

    def always_retry(db, gstin_id, period, **kw):
        seen.append(kw["attempt"])
        return sync.SyncOutcome(sync.SyncStatus.RETRY, "blip", retry_in=1)

    with mock.patch.object(tasks.sync, "sync_gstr2b", always_retry), \
         mock.patch.object(tasks, "SessionLocal", _session):
        result = tasks.fetch_gstr2b_task.apply(args=["g1", PERIOD])
    assert seen == [1, 2, 3, 4, 5, 6], "exactly six attempts, never a seventh"
    assert result.failed() or result.state != "SUCCESS"


# ---------------------------------------------------------------------------
# The real task through the real service
# ---------------------------------------------------------------------------

def _run_task(w):
    return tasks.fetch_gstr2b_task.apply(args=[w.gstin.id, PERIOD])


def test_a_flaky_gstn_recovers_without_anyone_being_alerted(db, fake, wired):
    w = World(db)
    fake.fetch_error = [GSPUnavailableError("blip"), ReturnNotAvailableError("not yet")]
    result = _run_task(w)

    assert result.result == "SUCCESS"
    statuses = [a.status.value for a in w.attempts()]
    assert statuses == ["RETRYING", "RETRYING", "SUCCESS"]
    assert [a.attempt_number for a in w.attempts()] == [1, 2, 3]
    assert len(fake.called("fetch_gstr2b")) == 3
    assert w.unread("associate", NotificationType.GSTR2B_FETCH_FAILED) == []
    assert len(w.unread("associate", NotificationType.GSTR2B_FETCH_COMPLETED)) == 1


def test_a_gstn_outage_that_never_clears_fails_loudly_after_six_attempts(db, fake, wired):
    w = World(db)
    fake.fetch_error = [GSPUnavailableError("GSTN is not responding.") for _ in range(20)]
    _run_task(w)

    statuses = [a.status.value for a in w.attempts()]
    assert statuses == ["RETRYING"] * 5 + ["FAILED"]
    assert len(fake.called("fetch_gstr2b")) == 6
    (alert,) = w.unread("associate", NotificationType.GSTR2B_FETCH_FAILED)
    assert "Gave up after 6" in alert.message and "not responding" in alert.message


def test_an_expired_consent_is_not_retried_and_never_reaches_the_provider(db, fake, wired):
    from datetime import datetime, timedelta
    w = World(db)
    w.gstin.consent_expires_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    _run_task(w)

    (attempt,) = w.attempts()
    assert attempt.status.value == "FAILED" and attempt.error_code == "CONSENT_EXPIRED"
    assert fake.calls == []
    assert len(w.unread("associate", NotificationType.GSP_CONSENT_EXPIRED)) == 1


def test_a_permanent_gsp_error_fails_at_once_and_says_why(db, fake, wired):
    w = World(db)
    fake.fetch_error = GSPResponseError("The statement was malformed.", transaction_id="txn-9")
    _run_task(w)

    (attempt,) = w.attempts()
    assert attempt.status.value == "FAILED" and attempt.attempt_number == 1
    assert len(fake.called("fetch_gstr2b")) == 1, "a permanent error must not be retried"
    (alert,) = w.unread("associate", NotificationType.GSTR2B_FETCH_FAILED)
    assert "malformed" in alert.message and "txn-9" in alert.message


def test_a_success_reconciles_and_notifies_in_one_pass(db, fake, wired):
    w = World(db)
    w.add_purchase_register()
    _run_task(w)

    w.refresh()
    assert db.query(models.ReconciliationRun).count() == 1
    (attempt,) = w.attempts()
    assert attempt.reconciliation_run_id is not None and attempt.status.value == "SUCCESS"


# ---------------------------------------------------------------------------
# The dispatcher task
# ---------------------------------------------------------------------------

def test_the_dispatcher_queues_one_fetch_per_eligible_gstin(db, fake, wired):
    live = World(db, slug="live")
    World(db, slug="off", gstin="29AAGCB7383J1Z4", connected=False)

    with mock.patch.object(tasks.fetch_gstr2b_task, "delay") as delay:
        report = tasks.dispatch_due_fetches_task()

    delay.assert_called_once_with(live.gstin.id, sync.previous_period())
    assert report["enqueued"] == 1 and report["not_connected"] == 1


def test_ping_round_trips_the_task_machinery():
    assert tasks.ping.apply().result == "pong"
