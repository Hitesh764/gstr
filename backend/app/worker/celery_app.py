"""
The background worker and its schedule.

    worker   celery -A app.worker.celery_app worker --pool=solo -l info
    beat     celery -A app.worker.celery_app beat -l info

`--pool=solo` is required on Windows, where Celery's default prefork pool does
not work. On Linux the default is fine and faster; drop the flag there.

What runs, and when (all times Asia/Kolkata):

    dispatch_due_fetches   06:00 on the 14th, 15th and 16th of each month.
                           GSTN generates GSTR-2B on the 14th; the 15th and 16th
                           are second and third chances that skip anyone already
                           fetched.
    refresh_tokens         every 30 minutes. Renews taxpayer tokens (6h life)
                           while the consent window behind them is still open.
    consent_health         every 2 hours. Warns before a consent lapses or before
                           it would miss the fetch window. Notifies once per cause.
    notice_deadline_alerts daily at NOTICE_ALERT_HOUR (08:00). Alerts on open GST
                           notices 7, 3 and 1 days out, on the day, and once when
                           overdue. Each alert is sent once per person.

The schedule is configuration (GSTR2B_FETCH_DAYS, GSTR2B_FETCH_HOUR), not code,
so changing when fetching starts does not need a deploy.

Reliability settings worth understanding:

    task_acks_late + task_reject_on_worker_lost
        A task is acknowledged only after it finishes, and is redelivered if the
        worker dies mid-run. Combined with the fetch being idempotent, a crash
        costs a retry, not a lost fetch.
    worker_prefetch_multiplier = 1
        A worker takes one task at a time, so a slow GSTN response does not
        leave a queue of tasks stuck behind it on one worker.
    task_ignore_result
        Outcomes are written to our own gstr2b_fetch_attempts table, where a
        person can read them. Celery's result store is not the system of record.
"""
from celery import Celery
from celery.schedules import crontab

from app.config import settings


def build_beat_schedule() -> dict:
    return {
        "gstr2b-dispatch-due-fetches": {
            "task": "gstr2b.dispatch_due_fetches",
            "schedule": crontab(
                minute=0, hour=settings.gstr2b_fetch_hour,
                day_of_month=settings.gstr2b_fetch_days.replace(" ", ""),
            ),
        },
        "gsp-refresh-tokens": {
            "task": "gstr2b.refresh_tokens",
            "schedule": crontab(minute="*/30"),
        },
        "gsp-consent-health": {
            "task": "gstr2b.consent_health",
            "schedule": crontab(minute=5, hour="*/2"),
        },
        "notice-deadline-alerts": {
            "task": "notices.deadline_alerts",
            "schedule": crontab(minute=0, hour=settings.notice_alert_hour),
        },
    }


celery_app = Celery(
    "gstr2b",
    broker=settings.celery_broker_url,
    backend=settings.celery_broker_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    timezone=settings.celery_timezone,
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_ignore_result=True,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    task_default_queue="gstr2b",
    beat_schedule=build_beat_schedule(),
)
