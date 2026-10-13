"""
Runs the notice deadline-alert job once, optionally pretending it is another day.

    python scripts/run_notice_alerts.py                    # as of today
    python scripts/run_notice_alerts.py --today 2026-09-28 # as of that date

In production the Celery beat schedule runs this at 08:00 IST every day. This
script is for seeing the alerts without waiting for real time to pass: upload a
notice with a deadline, then run it for the days 7, 3 and 1 before, the day
itself, and after.

Each alert is sent to a person once, so running it twice for the same day does
nothing the second time. That is the behaviour to check, not a fault.
"""
import argparse
import os
import sys
from datetime import date

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.database import SessionLocal  # noqa: E402
from app.services import notices as notices_svc  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--today", type=date.fromisoformat, default=None,
                        help="pretend today is this date (YYYY-MM-DD)")
    args = parser.parse_args()

    today = args.today or notices_svc.today_ist()
    with SessionLocal() as db:
        report = notices_svc.send_deadline_alerts(db, today=today)
    print(f"As of {today}: looked at {report.scanned} open notices with a deadline; "
          f"sent {report.alerted} alerts for {report.notices} notices.")


if __name__ == "__main__":
    main()
