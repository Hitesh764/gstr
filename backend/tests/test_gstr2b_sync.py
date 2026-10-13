"""
The GSTR-2B fetch: success, expiry, GSP errors, retry, and the nightly fan-out.

These call the service functions directly, with a scripted provider, so each
path is exercised without a broker or a network. The assertions worth reading
closely are the negative ones: that the provider was NOT called when consent
had ended, that a transient fault did NOT notify anyone, and that a second
lapse notifies again while a repeat of the same one does not.
"""
import random
from datetime import datetime, timedelta

import pytest

from app import models
from app.crypto import decrypt
from app.gsp.base import (
    ConsentExpiredError, GSPRateLimitedError, GSPResponseError, GSPUnavailableError,
    ReturnNotAvailableError,
)
from app.notifications import NotificationType
from app.services import consent as consent_svc
from app.services.gstr2b_sync import (
    SyncStatus, backoff_seconds, check_consent_health, dispatch_due_fetches,
    previous_period, refresh_expiring_tokens, sync_gstr2b,
)
from tests.gsp_helpers import (
    GSTIN, PERIOD, VENDOR, VENDOR_2, FakeGSP, World, make_document, make_invoice,
)

SCHEDULED = models.FetchTrigger.SCHEDULED
MANUAL = models.FetchTrigger.MANUAL


@pytest.fixture
def fake():
    return FakeGSP()


def run(db, w, fake, **kw):
    kw.setdefault("trigger", SCHEDULED)
    return sync_gstr2b(db, w.gstin.id, kw.pop("period", PERIOD), provider=fake, **kw)


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------

def test_a_successful_fetch_is_stored_as_an_api_version(db, fake):
    w = World(db)
    out = run(db, w, fake)

    assert out.status is SyncStatus.SUCCESS and out.invoice_count == 1
    w.refresh()
    fetch = db.query(models.GSTR2BFetch).one()
    assert fetch.source == models.FetchSource.API
    assert fetch.gsp_provider == "fake"
    assert fetch.is_current and fetch.version == 1
    assert db.query(models.GSTR2BEntry).count() == 1


def test_the_provider_is_called_with_the_stored_token_not_a_fresh_login(db, fake):
    w = World(db)
    run(db, w, fake)
    (call,) = fake.called("fetch_gstr2b")
    assert call[1:] == (GSTIN, PERIOD, "stored-token")


def test_fetch_auto_reconciles_against_the_current_purchase_register(db, fake):
    w = World(db)
    w.add_purchase_register()
    out = run(db, w, fake)

    assert out.reconciled is True and out.run_id
    w.refresh()
    run_row = db.query(models.ReconciliationRun).one()
    assert run_row.run_by is None, "a scheduled run records no user, not a made-up one"
    assert run_row.fetch_id == out.fetch_id
    statuses = {r.match_status.value for r in db.query(models.ReconciliationResult).all()}
    assert statuses == {"MATCHED"}


def test_without_a_purchase_register_the_fetch_is_kept_but_nothing_is_reconciled(db, fake):
    w = World(db)
    out = run(db, w, fake)

    assert out.status is SyncStatus.SUCCESS and out.reconciled is False
    assert "No purchase register" in out.message
    w.refresh()
    assert db.query(models.ReconciliationRun).count() == 0
    assert db.query(models.GSTR2BFetch).count() == 1


def test_reconciliation_uses_the_latest_purchase_register_version(db, fake):
    w = World(db)
    w.add_purchase_register(invoices=[make_invoice(taxable=99999.0)])       # v1: wrong value
    upload_v1 = db.query(models.PurchaseRegisterUpload).one()
    upload_v1.is_current = False
    db.commit()
    v2 = models.PurchaseRegisterUpload(
        firm_id=w.firm.id, client_gstin_id=w.gstin.id, period=PERIOD, version=2,
        is_current=True, status=models.UploadStatus.IMPORTED)
    db.add(v2)
    db.commit()
    w2_inv = make_invoice()
    from decimal import Decimal
    from datetime import date
    db.add(models.PurchaseRegisterEntry(
        firm_id=w.firm.id, upload_id=v2.id, vendor_name="Bharat Steel", vendor_gstin=VENDOR,
        invoice_number="INV-001", invoice_date=date(2026, 3, 5),
        taxable_value=Decimal(str(w2_inv["taxable_value"])), cgst=Decimal(str(w2_inv["cgst"])),
        sgst=Decimal(str(w2_inv["sgst"])), igst=Decimal("0"),
        total_value=Decimal(str(w2_inv["total_value"]))))
    db.commit()

    out = run(db, w, fake)
    assert out.reconciled
    w.refresh()
    assert db.query(models.ReconciliationRun).one().upload_id == v2.id


def test_itc_availability_from_the_provider_is_preserved(db, fake):
    w = World(db)
    fake.document = make_document(invoices=[
        make_invoice("A-1", itc=True), make_invoice("A-2", itc=False)])
    run(db, w, fake)
    w.refresh()
    flags = {e.invoice_number: e.itc_available for e in db.query(models.GSTR2BEntry).all()}
    assert flags == {"A-1": True, "A-2": False}


def test_a_refetch_creates_a_new_version_and_keeps_the_old(db, fake):
    w = World(db)
    run(db, w, fake)
    run(db, w, fake, force=True)
    w.refresh()
    fetches = db.query(models.GSTR2BFetch).order_by(models.GSTR2BFetch.version).all()
    assert [f.version for f in fetches] == [1, 2]
    assert [f.is_current for f in fetches] == [False, True]


def test_credit_notes_are_reported_but_not_reconciled(db, fake):
    w = World(db)
    fake.credit_notes = 2
    out = run(db, w, fake)
    assert out.credit_note_count == 2
    assert "2 credit/debit note" in out.message and "not yet reconciled" in out.message


def test_success_notifies_the_assigned_associate_once(db, fake):
    w = World(db)
    run(db, w, fake)
    got = w.unread("associate", NotificationType.GSTR2B_FETCH_COMPLETED)
    assert len(got) == 1
    assert "GSTR-2B received" in got[0].title and PERIOD in got[0].message
    assert w.unread("manager", NotificationType.GSTR2B_FETCH_COMPLETED) == []


def test_unassigned_client_falls_back_to_approvers_rather_than_nobody(db, fake):
    w = World(db, assign_associate=False)
    run(db, w, fake)
    assert len(w.unread("manager", NotificationType.GSTR2B_FETCH_COMPLETED)) == 1
    assert len(w.unread("owner", NotificationType.GSTR2B_FETCH_COMPLETED)) == 1
    assert w.unread("associate", NotificationType.GSTR2B_FETCH_COMPLETED) == []


def test_every_outcome_writes_an_attempt_row_a_person_can_read(db, fake):
    w = World(db)
    run(db, w, fake)
    (attempt,) = w.attempts()
    assert attempt.status == models.FetchAttemptStatus.SUCCESS
    assert attempt.trigger == SCHEDULED and attempt.invoice_count == 1
    assert attempt.provider_transaction_id == "txn-2b"


def test_a_successful_fetch_is_audited(db, fake):
    w = World(db)
    run(db, w, fake)
    entry = db.query(models.AuditLog).filter_by(action="FETCH_GSTR2B").one()
    assert entry.firm_id == w.firm.id and entry.user_id is None
    assert entry.meta["source"] == "API" and entry.meta["trigger"] == "SCHEDULED"


# ---------------------------------------------------------------------------
# Consent gate: the provider must not be contacted without live consent
# ---------------------------------------------------------------------------

def test_never_connected_is_skipped_silently_on_a_schedule(db, fake):
    w = World(db, connected=False)
    out = run(db, w, fake)
    assert out.status is SyncStatus.SKIPPED
    assert fake.calls == []
    assert w.attempts() == [], "manual-upload firms must not accumulate failure rows"
    assert db.query(models.Notification).count() == 0


def test_manual_fetch_without_a_connection_says_so_and_notifies_nobody(db, fake):
    w = World(db, connected=False)
    out = run(db, w, fake, trigger=MANUAL, max_attempts=1)
    assert out.status is SyncStatus.FAILED and out.error_code == "NOT_CONNECTED"
    assert fake.calls == []
    assert db.query(models.Notification).count() == 0


def test_expired_consent_blocks_the_fetch_and_never_contacts_the_provider(db, fake):
    w = World(db)
    w.gstin.consent_expires_at = datetime.utcnow() - timedelta(hours=2)
    db.commit()

    out = run(db, w, fake)
    assert out.status is SyncStatus.FAILED and out.error_code == "CONSENT_EXPIRED"
    assert fake.calls == [], "no call may be made once consent has ended"
    assert "not fetched" in out.message and PERIOD in out.message
    (attempt,) = w.attempts()
    assert attempt.status == models.FetchAttemptStatus.FAILED
    assert attempt.error_code == "CONSENT_EXPIRED"


def test_expiry_notifies_once_per_lapse_not_on_every_run(db, fake):
    w = World(db)
    w.gstin.consent_expires_at = datetime.utcnow() - timedelta(hours=2)
    db.commit()

    run(db, w, fake)
    run(db, w, fake)
    run(db, w, fake)
    assert len(w.unread("associate", NotificationType.GSP_CONSENT_EXPIRED)) == 1
    assert len(w.attempts()) == 3, "every attempt is still recorded"


def test_a_later_lapse_notifies_again(db, fake):
    w = World(db)
    w.gstin.consent_expires_at = datetime.utcnow() - timedelta(days=40)
    db.commit()
    run(db, w, fake)

    w.connect()                                  # renewed...
    w.gstin.consent_expires_at = datetime.utcnow() - timedelta(hours=1)   # ...and lapsed again
    db.commit()
    run(db, w, fake)
    assert len(w.unread("associate", NotificationType.GSP_CONSENT_EXPIRED)) == 2


def test_the_provider_ending_a_session_early_marks_consent_expired_today(db, fake):
    """If the taxpayer revokes access on the GST portal, our recorded end date
    (still in the future) must not survive, or the state reads ACTIVE with no
    token behind it and the alert claims access ended on a future date."""
    w = World(db)
    fake.fetch_error = ConsentExpiredError("Session ended.")
    out = run(db, w, fake)

    assert out.status is SyncStatus.FAILED and out.error_code == "CONSENT_EXPIRED"
    w.refresh()
    assert w.gstin.gsp_consent_token is None
    assert consent_svc.consent_state(w.gstin) is consent_svc.ConsentState.EXPIRED
    assert w.gstin.consent_expires_at <= datetime.utcnow()
    assert len(w.unread("associate", NotificationType.GSP_CONSENT_EXPIRED)) == 1


def test_a_token_from_a_different_provider_is_never_sent(db, fake):
    w = World(db, provider="sandbox")            # connected via sandbox; active is "fake"
    out = run(db, w, fake)
    assert out.status is SyncStatus.FAILED
    assert fake.calls == []
    assert "sandbox" in out.message and "fake" in out.message


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

def test_a_token_near_expiry_is_refreshed_before_the_fetch(db, fake):
    w = World(db)
    w.connect(token_hours=0.5)                   # 30 min left; margin is 90
    db.commit()

    out = run(db, w, fake)
    assert out.status is SyncStatus.SUCCESS
    assert [c[0] for c in fake.calls] == ["refresh", "fetch_gstr2b"]
    assert fake.called("refresh")[0][1] == "stored-token"
    assert fake.called("fetch_gstr2b")[0][3] == "fake-token-1", "fetch must use the refreshed token"

    w.refresh()
    assert decrypt(w.gstin.gsp_consent_token) == "fake-token-1"
    assert w.gstin.gsp_consent_token != "fake-token-1", "stored encrypted, never as plaintext"


def test_a_fresh_token_is_not_refreshed(db, fake):
    w = World(db)
    run(db, w, fake)
    assert fake.called("refresh") == []


def test_a_refresh_glitch_does_not_fail_a_fetch_the_live_token_can_serve(db, fake):
    w = World(db)
    w.connect(token_hours=0.5)
    db.commit()
    fake.refresh_error = GSPUnavailableError("GSTN blip")

    out = run(db, w, fake)
    assert out.status is SyncStatus.SUCCESS
    assert fake.called("fetch_gstr2b")[0][3] == "stored-token"


def test_a_refresh_glitch_on_an_already_dead_token_is_retried(db, fake):
    w = World(db)
    w.connect()
    w.gstin.gsp_token_expires_at = datetime.utcnow() - timedelta(minutes=5)
    db.commit()
    fake.refresh_error = GSPUnavailableError("GSTN blip")

    out = run(db, w, fake)
    assert out.status is SyncStatus.RETRY
    assert fake.called("fetch_gstr2b") == []


def test_a_refresh_refused_because_the_session_ended_is_consent_expired(db, fake):
    w = World(db)
    w.connect(token_hours=0.5)
    db.commit()
    fake.refresh_error = ConsentExpiredError("Session over.")

    out = run(db, w, fake)
    assert out.status is SyncStatus.FAILED and out.error_code == "CONSENT_EXPIRED"
    assert fake.called("fetch_gstr2b") == []
    w.refresh()
    assert w.gstin.gsp_consent_token is None


# ---------------------------------------------------------------------------
# Failure handling and retry
# ---------------------------------------------------------------------------

def test_a_transient_failure_schedules_a_retry_and_tells_nobody_yet(db, fake):
    w = World(db)
    fake.fetch_error = GSPUnavailableError("GSTN is not responding.")
    out = run(db, w, fake, attempt=1, max_attempts=6, rng=random.Random(1))

    assert out.status is SyncStatus.RETRY and out.retry_in >= 60
    (attempt,) = w.attempts()
    assert attempt.status == models.FetchAttemptStatus.RETRYING
    assert attempt.next_retry_at is not None and attempt.attempt_number == 1
    assert "1 of 6" in attempt.message
    assert db.query(models.Notification).count() == 0, "a blip that may resolve is not an alert"


def test_backoff_grows_between_attempts(db, fake):
    w = World(db)
    delays = []
    for attempt in (1, 2, 3, 4):
        fake.fetch_error = GSPUnavailableError("down")
        delays.append(run(db, w, fake, attempt=attempt, max_attempts=6).retry_in)
    assert delays == sorted(delays) and delays[-1] >= delays[0] * 4


def test_the_final_attempt_fails_for_good_and_notifies_with_the_reason(db, fake):
    w = World(db)
    fake.fetch_error = GSPUnavailableError("GSTN is not responding.")
    out = run(db, w, fake, attempt=6, max_attempts=6)

    assert out.status is SyncStatus.FAILED and out.error_code == "GSP_UNAVAILABLE"
    assert "Gave up after 6 attempt(s)" in out.message and "GSTN is not responding" in out.message
    (note,) = w.unread("associate", NotificationType.GSTR2B_FETCH_FAILED)
    assert "GSTN is not responding" in note.message and PERIOD in note.message


def test_a_non_retryable_error_fails_immediately_with_its_reference(db, fake):
    w = World(db)
    fake.fetch_error = GSPResponseError("Unexpected shape.", transaction_id="txn-abc")
    out = run(db, w, fake, attempt=1, max_attempts=6)

    assert out.status is SyncStatus.FAILED and out.retry_in is None
    assert "txn-abc" in out.message, "a support reference must reach the person"
    assert len(w.unread("associate", NotificationType.GSTR2B_FETCH_FAILED)) == 1


def test_gstr2b_not_generated_yet_is_retried(db, fake):
    w = World(db)
    fake.fetch_error = ReturnNotAvailableError("Not generated yet.")
    assert run(db, w, fake, attempt=1, max_attempts=6).status is SyncStatus.RETRY


def test_a_rate_limit_hint_sets_a_floor_on_the_wait(db, fake):
    w = World(db)
    fake.fetch_error = GSPRateLimitedError("Slow down.", retry_after=1800)
    out = run(db, w, fake, attempt=1, max_attempts=6)
    assert out.status is SyncStatus.RETRY and out.retry_in >= 1800


def test_a_manual_fetch_reports_a_transient_failure_instead_of_retrying_in_the_dark(db, fake):
    w = World(db)
    fake.fetch_error = GSPUnavailableError("GSTN is not responding.")
    out = run(db, w, fake, trigger=MANUAL, attempt=1, max_attempts=1)
    assert out.status is SyncStatus.FAILED
    assert "Gave up" not in out.message and "GSTN is not responding" in out.message
    assert db.query(models.Notification).count() == 0, "the person is already looking at the answer"


def test_an_unexpected_bug_is_recorded_notified_and_logged_not_swallowed(db, fake, caplog):
    w = World(db)
    fake.fetch_error = RuntimeError("kaboom")
    out = run(db, w, fake)

    assert out.status is SyncStatus.FAILED and out.error_code == "UNEXPECTED"
    assert len(w.unread("associate", NotificationType.GSTR2B_FETCH_FAILED)) == 1
    assert any("Unexpected error fetching GSTR-2B" in r.getMessage() for r in caplog.records)


def test_a_failed_fetch_leaves_earlier_data_untouched(db, fake):
    w = World(db)
    run(db, w, fake)
    fake.fetch_error = GSPResponseError("bad")
    run(db, w, fake, force=True)
    w.refresh()
    (fetch,) = db.query(models.GSTR2BFetch).all()
    assert fetch.is_current and fetch.version == 1


def test_an_already_fetched_period_is_skipped_without_calling_the_provider(db, fake):
    w = World(db)
    run(db, w, fake)
    fake.calls.clear()

    out = run(db, w, fake)
    assert out.status is SyncStatus.SKIPPED and fake.calls == []


def test_an_inactive_client_is_skipped(db, fake):
    w = World(db)
    w.client.status = models.ClientStatus.PAUSED
    db.commit()
    assert run(db, w, fake).status is SyncStatus.SKIPPED and fake.calls == []


def test_a_deleted_gstin_is_skipped(db, fake):
    w = World(db)
    w.gstin.deleted_at = datetime.utcnow()
    db.commit()
    assert run(db, w, fake).status is SyncStatus.SKIPPED and fake.calls == []


# ---------------------------------------------------------------------------
# Backoff and period arithmetic
# ---------------------------------------------------------------------------

def test_backoff_doubles_and_is_capped_at_an_hour():
    rng = random.Random(0)
    zero_jitter = type("R", (), {"random": staticmethod(lambda: 0.0)})
    assert [backoff_seconds(n, rng=zero_jitter) for n in range(1, 8)] == \
        [60, 120, 240, 480, 960, 1920, 3600]
    for n in range(1, 10):
        base = backoff_seconds(n, rng=zero_jitter)
        assert base <= backoff_seconds(n, rng=rng) <= int(base * 1.1) + 1


@pytest.mark.parametrize("utc_now,expected", [
    (datetime(2026, 4, 14, 0, 30), "2026-03"),     # 06:00 IST on the 14th
    (datetime(2026, 4, 30, 12, 0), "2026-03"),
    (datetime(2026, 1, 14, 0, 30), "2025-12"),     # January wraps to last December
    (datetime(2026, 4, 30, 20, 0), "2026-04"),     # 01:30 IST on 1 May: IST is already May
])
def test_previous_period_uses_indian_calendar_dates(utc_now, expected):
    assert previous_period(utc_now) == expected


# ---------------------------------------------------------------------------
# The nightly fan-out
# ---------------------------------------------------------------------------

THE_14TH = datetime(2026, 4, 14, 0, 30)          # 06:00 IST


def test_dispatch_queues_exactly_the_gstins_that_should_be_fetched(db, fake):
    live = World(db, slug="live", gstin=GSTIN)
    done = World(db, slug="done", gstin="29AAGCB7383J1Z4")
    expired = World(db, slug="expired", gstin="07AAACH1925Q1ZS")
    paused = World(db, slug="paused", gstin="24AAACC4175D1ZE")

    now = THE_14TH
    for w in (live, done, expired, paused):
        w.connect(now=now)
    expired.gstin.consent_expires_at = now - timedelta(days=1)
    paused.client.status = models.ClientStatus.PAUSED
    db.commit()
    run(db, done, fake, period="2026-03", now=now)     # `done` already has its fetch

    queued = []
    report = dispatch_due_fetches(db, enqueue=lambda gid, per: queued.append((gid, per)), now=now)

    assert report.period == "2026-03"
    assert queued == [(live.gstin.id, "2026-03")]
    assert (report.enqueued, report.already_fetched, report.expired, report.inactive) == (1, 1, 1, 1)


def test_dispatch_serves_every_firm_but_never_a_deleted_one(db):
    a = World(db, slug="a", gstin=GSTIN)
    b = World(db, slug="b", gstin="29AAGCB7383J1Z4")
    gone = World(db, slug="gone", gstin="07AAACH1925Q1ZS")
    for w in (a, b, gone):
        w.connect(now=THE_14TH)
    gone.firm.deleted_at = datetime.utcnow()
    db.commit()

    queued = []
    dispatch_due_fetches(db, enqueue=lambda gid, per: queued.append(gid), now=THE_14TH)
    assert sorted(queued) == sorted([a.gstin.id, b.gstin.id])


def test_dispatch_tells_an_associate_about_expired_consent_once(db):
    w = World(db)
    w.connect(now=THE_14TH)
    w.gstin.consent_expires_at = THE_14TH - timedelta(days=2)
    db.commit()

    for _ in range(3):
        dispatch_due_fetches(db, enqueue=lambda *a: None, now=THE_14TH)
    assert len(w.unread("associate", NotificationType.GSP_CONSENT_EXPIRED)) == 1


def test_never_connected_gstins_are_not_nagged(db):
    w = World(db, connected=False)
    queued = []
    report = dispatch_due_fetches(db, enqueue=lambda gid, per: queued.append(gid), now=THE_14TH)
    assert queued == [] and report.not_connected == 1
    assert db.query(models.Notification).count() == 0


# ---------------------------------------------------------------------------
# Consent health and token upkeep
# ---------------------------------------------------------------------------

def test_expiring_consent_is_flagged_once(db):
    w = World(db)
    now = datetime(2026, 4, 1, 10, 0)
    w.connect(now=now - timedelta(days=20), session_hours=24 * 30)
    w.gstin.consent_expires_at = now + timedelta(hours=10)       # inside the last day
    db.commit()

    for _ in range(3):
        check_consent_health(db, now=now)
    got = w.unread("associate", NotificationType.GSP_CONSENT_EXPIRING)
    assert len(got) == 1 and "10 hours" in got[0].message


def test_a_consent_that_will_miss_the_fetch_window_is_flagged_days_ahead(db):
    """A 30-day consent granted on 15 March ends on 14 April, in the middle of
    the 14th-16th window. It should be flagged before the 14th, not found dead
    on the morning of it."""
    w = World(db)
    granted = datetime(2026, 3, 15, 6, 0)
    w.connect(now=granted, session_hours=24 * 30)                # ends 14 April
    db.commit()

    check_consent_health(db, now=datetime(2026, 4, 5, 6, 0))     # 9 days before the 14th
    assert w.unread("associate", NotificationType.GSP_CONSENT_EXPIRING) == []

    check_consent_health(db, now=datetime(2026, 4, 10, 6, 0))    # inside the 5-day lead
    got = w.unread("associate", NotificationType.GSP_CONSENT_EXPIRING)
    assert len(got) == 1 and "before the 14th" in got[0].title


def test_a_consent_that_outlives_the_window_is_not_flagged(db):
    w = World(db)
    w.connect(now=datetime(2026, 4, 8, 6, 0), session_hours=24 * 30)   # ends 8 May
    db.commit()
    check_consent_health(db, now=datetime(2026, 4, 10, 6, 0))
    assert w.unread("associate", NotificationType.GSP_CONSENT_EXPIRING) == []


def test_expired_consent_is_reported_by_the_health_check_too(db):
    w = World(db)
    now = datetime(2026, 4, 20, 9, 0)
    w.connect(now=now - timedelta(days=30))
    w.gstin.consent_expires_at = now - timedelta(hours=3)
    db.commit()
    check_consent_health(db, now=now)
    assert len(w.unread("associate", NotificationType.GSP_CONSENT_EXPIRED)) == 1


def test_refresh_job_renews_only_tokens_that_need_it(db, fake):
    needs = World(db, slug="needs", provider="fake")
    needs.connect(token_hours=0.5)
    fine = World(db, slug="fine", gstin="29AAGCB7383J1Z4", provider="fake")
    other = World(db, slug="other", gstin="07AAACH1925Q1ZS", provider="sandbox")
    other.connect(provider="sandbox", token_hours=0.5)
    db.commit()

    renewed = refresh_expiring_tokens(db, provider=fake)
    assert renewed == 1
    assert len(fake.called("refresh")) == 1, "only the token near expiry, and only ours"


def test_refresh_job_drops_a_token_whose_session_has_ended(db, fake):
    w = World(db)
    w.connect(token_hours=0.5)
    db.commit()
    fake.refresh_error = ConsentExpiredError("Session over.")

    refresh_expiring_tokens(db, provider=fake)
    w.refresh()
    assert w.gstin.gsp_consent_token is None
    assert consent_svc.consent_state(w.gstin) is consent_svc.ConsentState.EXPIRED


def test_refresh_job_survives_a_provider_outage(db, fake):
    w = World(db)
    w.connect(token_hours=0.5)
    db.commit()
    fake.refresh_error = GSPUnavailableError("down")
    assert refresh_expiring_tokens(db, provider=fake) == 0
    w.refresh()
    assert w.gstin.gsp_consent_token is not None, "an outage must not discard a good token"
