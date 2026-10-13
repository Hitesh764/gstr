"""
Notice deadline alerts: 7, 3 and 1 days out, on the day, and once when overdue.

`send_deadline_alerts` takes `today` as a parameter, so these tests walk a
notice through its whole approach to the deadline by moving the date, with no
sleeping and no patching of the clock.
"""
from datetime import date, datetime, timedelta
from unittest import mock

import pytest

from app import models
from app.notifications import NotificationType
from app.services import notices as svc
from app.worker import tasks
from app.worker.celery_app import build_beat_schedule
from tests.gsp_helpers import World

DEADLINE = date(2026, 6, 30)
ALERT = NotificationType.NOTICE_DEADLINE_APPROACHING


def days_before(n: int) -> date:
    return DEADLINE - timedelta(days=n)


@pytest.fixture
def w(db):
    return World(db, slug="al", connected=False)


def make_notice(w, *, deadline=DEADLINE, status=models.NoticeStatus.UNDER_REVIEW,
                assigned="associate", title="ASMT-10 for March 2026"):
    n = models.Notice(
        firm_id=w.firm.id, client_id=w.client.id, title=title, response_deadline=deadline,
        status=status, assigned_to=w.users[assigned].id if assigned else None,
    )
    w.db.add(n)
    w.db.commit()
    return n


def alerts_for(w, user_key):
    w.db.expire_all()
    return [n for n in w.unread(user_key, ALERT)]


def run(w, today):
    return svc.send_deadline_alerts(w.db, today=today)


# ---------------------------------------------------------------------------

def test_the_assignee_is_alerted_seven_days_out(w):
    make_notice(w)
    report = run(w, days_before(7))
    rows = alerts_for(w, "associate")
    assert len(rows) == 1 and report.alerted == 1 and report.notices == 1
    assert rows[0].title == "A notice response is due in 7 days"
    assert "30-06-2026" in rows[0].message and "under review" in rows[0].message
    assert rows[0].link.startswith("/notices/")
    assert alerts_for(w, "manager") == [] and alerts_for(w, "owner") == []   # not yet escalated


def test_nothing_is_sent_before_the_first_alert_point(w):
    make_notice(w)
    assert run(w, days_before(8)).alerted == 0
    assert run(w, days_before(30)).alerted == 0


def test_running_the_job_again_never_repeats_an_alert(w):
    make_notice(w)
    run(w, days_before(7))
    again = run(w, days_before(7))
    assert again.alerted == 0 and len(alerts_for(w, "associate")) == 1


def test_days_between_alert_points_stay_quiet(w):
    make_notice(w)
    run(w, days_before(7))
    for days in (6, 5, 4):
        assert run(w, days_before(days)).alerted == 0
    assert len(alerts_for(w, "associate")) == 1


def test_the_whole_approach_produces_exactly_one_alert_per_point(w):
    make_notice(w)
    for days in range(10, -3, -1):
        run(w, days_before(days))
    titles = [r.title for r in alerts_for(w, "associate")]
    assert sorted(titles) == sorted([
        "A notice response is due in 7 days",
        "A notice response is due in 3 days",
        "A notice response is due in 1 day",
        "A notice response is due today",
        "A notice response is 1 day overdue",
    ])


def test_it_escalates_to_managers_and_partners_at_one_day_and_beyond(w):
    make_notice(w)
    run(w, days_before(3))
    assert alerts_for(w, "manager") == []
    run(w, days_before(1))
    for who in ("associate", "manager", "owner"):
        assert any("1 day" in r.title for r in alerts_for(w, who)), who


def test_due_today_and_overdue_reach_everyone_responsible_and_overdue_fires_once(w):
    make_notice(w)
    run(w, DEADLINE)
    assert any(r.title == "A notice response is due today" for r in alerts_for(w, "owner"))
    run(w, DEADLINE + timedelta(days=4))
    run(w, DEADLINE + timedelta(days=5))
    overdue = [r for r in alerts_for(w, "manager") if "overdue" in r.title]
    assert len(overdue) == 1 and overdue[0].title == "A notice response is 4 days overdue"


def test_a_late_upload_reports_the_real_days_left_not_the_alert_point(w):
    make_notice(w)
    run(w, days_before(2))                       # crosses the 3-day point with 2 left
    assert alerts_for(w, "associate")[0].title == "A notice response is due in 2 days"


@pytest.mark.parametrize("status", [models.NoticeStatus.FILED, models.NoticeStatus.CLOSED])
def test_finished_notices_are_never_alerted(w, status):
    make_notice(w, status=status)
    assert run(w, days_before(1)).scanned == 0
    assert alerts_for(w, "associate") == []


@pytest.mark.parametrize("status", [
    models.NoticeStatus.RECEIVED, models.NoticeStatus.UNDER_REVIEW,
    models.NoticeStatus.RESPONSE_DRAFTED, models.NoticeStatus.PARTNER_APPROVED,
])
def test_every_open_stage_is_alerted(w, status):
    make_notice(w, status=status)
    assert run(w, days_before(3)).alerted == 1


def test_approved_but_unfiled_is_the_case_most_worth_alerting(w):
    make_notice(w, status=models.NoticeStatus.PARTNER_APPROVED)
    run(w, days_before(1))
    assert "partner approved" in alerts_for(w, "manager")[0].message


def test_deleted_and_undated_notices_are_skipped(w):
    gone = make_notice(w)
    gone.deleted_at = datetime.utcnow()
    make_notice(w, deadline=None)
    w.db.commit()
    assert run(w, days_before(1)).scanned == 0


def test_with_no_assignee_it_goes_to_the_clients_associate(w):
    make_notice(w, assigned=None)
    run(w, days_before(7))
    assert len(alerts_for(w, "associate")) == 1            # the client's assigned associate
    assert alerts_for(w, "manager") == []


def test_with_nobody_responsible_it_goes_to_the_managers_rather_than_no_one(w):
    w.client.assigned_associate_id = None
    make_notice(w, assigned=None)
    w.db.commit()
    run(w, days_before(7))
    assert alerts_for(w, "associate") == []
    assert len(alerts_for(w, "manager")) == 1 and len(alerts_for(w, "owner")) == 1


def test_a_deactivated_assignee_is_replaced_by_the_clients_owner(w):
    w.users["manager"].is_active = False
    n = make_notice(w, assigned="manager")
    w.db.commit()
    run(w, days_before(7))
    assert alerts_for(w, "manager") == []
    assert len(alerts_for(w, "associate")) == 1


def test_moving_the_deadline_starts_a_fresh_set_of_alerts(w):
    n = make_notice(w)
    run(w, days_before(7))
    n.response_deadline = DEADLINE + timedelta(days=30)        # extension granted
    w.db.commit()
    assert run(w, days_before(7)).alerted == 0                 # now 37 days out: quiet
    run(w, DEADLINE + timedelta(days=23))                      # 7 days before the new deadline
    assert len(alerts_for(w, "associate")) == 2


def test_a_person_who_turned_the_alert_off_is_not_sent_it(w):
    w.db.add(models.NotificationPreference(
        firm_id=w.firm.id, user_id=w.users["associate"].id, event_type=ALERT,
        in_app=False, email=False))
    make_notice(w)
    w.db.commit()
    run(w, days_before(7))
    assert alerts_for(w, "associate") == []


def test_alerts_never_cross_between_firms(db):
    a = World(db, slug="fa", gstin="27AAPFU0939F1ZV", connected=False)
    b = World(db, slug="fb", gstin="29AAGCB7383J1Z4", connected=False)
    make_notice(a)
    make_notice(b, title="Firm B notice")
    svc.send_deadline_alerts(db, today=days_before(1))
    db.expire_all()
    a_msgs = " ".join(m.message for who in a.users for m in a.unread(who, ALERT))
    b_msgs = " ".join(m.message for who in b.users for m in b.unread(who, ALERT))
    assert "Firm B notice" not in a_msgs and "Firm B notice" in b_msgs
    for who in a.users:
        for row in a.unread(who, ALERT):
            assert row.firm_id == a.firm.id


def test_the_alert_email_goes_out_by_default(w):
    """Deadline alerts are email-on by default, so they still reach someone who
    is not looking at the bell."""
    with mock.patch("app.notifications.messaging.get_email_provider") as provider:
        provider.return_value.send.return_value = mock.Mock(ok=True)
        make_notice(w)
        run(w, days_before(7))
    assert provider.return_value.send.called


# ---------------------------------------------------------------------------
# The scheduled job
# ---------------------------------------------------------------------------

def test_the_alerts_are_scheduled_daily_at_eight_ist():
    entry = build_beat_schedule()["notice-deadline-alerts"]
    assert entry["task"] == "notices.deadline_alerts"
    assert entry["schedule"].hour == {8} and entry["schedule"].minute == {0}


def test_the_task_runs_the_service_against_the_real_database(w, SessionFactory):
    make_notice(w, deadline=svc.today_ist() + timedelta(days=3))
    with mock.patch.object(tasks, "SessionLocal", SessionFactory):
        result = tasks.notice_deadline_alerts_task.apply()
    assert result.result["alerted"] == 1 and result.result["scanned"] == 1
    assert len(alerts_for(w, "associate")) == 1
