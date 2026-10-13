"""
Seeds the local database with a demo firm, staff, a client and one fully
reconciled period, so the app has something to look at immediately.

Creates:
  - 1 firm (Sharma & Associates)
  - 1 demo user with the Owner role
  - 1 client with one GSTIN
  - a purchase register upload + GSTR-2B fetch for 2026-03
  - a completed reconciliation run with its exception queue

Run: python scripts/seed.py

Set SEED_EXTRA_STAFF=1 to also create a manager and an associate, which is
needed to demonstrate the approval chain: the app refuses to let anyone
approve a resolution they wrote themselves.
"""
import json
import os
import sys

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.config import SAMPLE_DATA_DIR  # noqa: E402
from app.database import SessionLocal, engine  # noqa: E402
from app import audit, models  # noqa: E402
from app.audit import AuditAction  # noqa: E402
from app.ingestion.gstr2b import get_sample_gstr2b_doc, parse_gstr2b_json  # noqa: E402
from app.ingestion.purchase_register import parse_purchase_register_file  # noqa: E402
from app.matching.engine import MatchTolerances, reconcile  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.validation import state_from_gstin  # noqa: E402

PERIOD = "2026-03"
DEMO_EMAIL = "demo@sharma-ca.in"
DEMO_PASSWORD = "demo-password-123"

AT_RISK = ("SUPPLIER_NOT_FILED", "VALUE_MISMATCH", "CREDIT_NOTE_DISCREPANCY")
FLAGGED = AT_RISK + ("MISSING_IN_BOOKS",)


def reset_schema():
    """Wipe the database and rebuild it from the real migrations.

    Built from `alembic upgrade head` rather than `create_all()` on purpose.
    create_all() knows nothing about the parts of the schema that only exist
    in migrations (the audit-log trigger, for one), so a seeded database would
    quietly differ from a deployed one while claiming to be at the same
    revision.

    This DESTROYS everything, so it refuses to run outside development.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import MetaData, text

    from app.config import settings

    if settings.environment != "development":
        raise SystemExit(
            f"Refusing to reset the database: ENVIRONMENT={settings.environment!r}. "
            "This script drops every table and is for local development only."
        )

    print("Resetting database...")
    if engine.dialect.name == "postgresql":
        # Dropping the schema takes the tables, the alembic_version stamp and
        # the audit-log trigger function with it, leaving nothing stale.
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        # SQLite does not enforce foreign keys, so drop order is irrelevant.
        meta = MetaData()
        meta.reflect(bind=engine)
        with engine.begin() as conn:
            for name in meta.tables:
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))

    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    command.upgrade(cfg, "head")
    print("Schema rebuilt from migrations at alembic head.")


def main():
    reset_schema()
    db = SessionLocal()
    try:
        # --- firm + staff --------------------------------------------------
        firm = models.Firm(
            name="Sharma & Associates",
            contact_email=DEMO_EMAIL,
            contact_phone="+919876543210",
            subscription_tier=models.SubscriptionTier.GROWTH,
            subscription_status=models.SubscriptionStatus.ACTIVE,
        )
        db.add(firm)
        db.flush()

        # One demo account, with the Owner role so every screen is reachable.
        #
        # Note this means the approval step cannot be demonstrated on its own:
        # a resolution must be approved by someone other than whoever wrote it,
        # so a single account will be refused at that point by design. Invite a
        # second user from the app, or set SEED_EXTRA_STAFF=1 to have this
        # script create a manager and an associate as well.
        pw = hash_password(DEMO_PASSWORD)
        demo_user = models.User(firm_id=firm.id, name="Anita Sharma",
                                email=DEMO_EMAIL, role=models.UserRole.OWNER,
                                password_hash=pw, email_verified=True)
        db.add(demo_user)
        db.flush()

        staff = [demo_user]
        if os.environ.get("SEED_EXTRA_STAFF") == "1":
            manager = models.User(firm_id=firm.id, name="Rohit Desai",
                                  email="manager@sharma-ca.in",
                                  role=models.UserRole.MANAGER,
                                  password_hash=pw, email_verified=True)
            associate = models.User(firm_id=firm.id, name="Priya Nair",
                                    email="associate@sharma-ca.in",
                                    role=models.UserRole.ASSOCIATE,
                                    password_hash=pw, email_verified=True)
            db.add_all([manager, associate])
            db.flush()
            staff += [manager, associate]

        # Everything the seed records is attributed to the demo user.
        actor = demo_user

        db.add(models.Subscription(firm_id=firm.id, tier=firm.subscription_tier,
                                   status=firm.subscription_status))
        audit.record(db, firm_id=firm.id, user_id=actor.id,
                     action=AuditAction.FIRM_SIGNUP, entity_type="Firm",
                     entity_id=firm.id, metadata={"seeded": True})
        print(f"Created firm: {firm.name} with {len(staff)} user(s)")

        # --- client + GSTIN --------------------------------------------------
        sample_doc = get_sample_gstr2b_doc()
        client_gstin_value = sample_doc["gstin"]

        client = models.Client(
            firm_id=firm.id, name="Shree Ganesh Manufacturing Co.",
            entity_type=models.EntityType.COMPANY,
            engagement_type=models.EngagementType.BOTH,
            status=models.ClientStatus.ACTIVE,
            contact_name="Mahesh Patil",
            contact_email="accounts@shreeganesh.in",
            assigned_associate_id=actor.id,
        )
        db.add(client)
        db.flush()

        code, state = state_from_gstin(client_gstin_value)
        gstin = models.ClientGSTIN(
            firm_id=firm.id, client_id=client.id, gstin=client_gstin_value,
            state_code=code, state_name=state, is_primary=True,
        )
        db.add(gstin)
        db.flush()
        print(f"Created client: {client.name} ({gstin.gstin}, {state})")

        # --- purchase register upload ------------------------------------------
        csv_path = os.path.join(SAMPLE_DATA_DIR, "purchase_register_sample.csv")
        with open(csv_path, "rb") as f:
            outcome = parse_purchase_register_file(
                f.read(), "purchase_register_sample.csv", period=PERIOD
            )
        if outcome.errors:
            print(f"  WARNING: {len(outcome.errors)} row error(s) in sample data:")
            for e in outcome.errors[:5]:
                print(f"    row {e.row}: {e.message}")

        upload = models.PurchaseRegisterUpload(
            firm_id=firm.id, client_gstin_id=gstin.id, period=PERIOD, version=1,
            is_current=True, source="MANUAL_UPLOAD",
            original_filename="purchase_register_sample.csv",
            status=models.UploadStatus.IMPORTED,
            row_count=len(outcome.rows), error_count=len(outcome.errors),
            uploaded_by=actor.id,
        )
        db.add(upload)
        db.flush()
        for row in outcome.rows:
            db.add(models.PurchaseRegisterEntry(firm_id=firm.id, upload_id=upload.id, **row))
        audit.record(db, firm_id=firm.id, user_id=actor.id,
                     action=AuditAction.UPLOAD_PURCHASE_REGISTER,
                     entity_type="PurchaseRegisterUpload", entity_id=upload.id,
                     metadata={"rows": len(outcome.rows), "period": PERIOD})
        print(f"Loaded {len(outcome.rows)} purchase register entries "
              f"({len(outcome.warnings)} warnings)")

        # --- GSTR-2B fetch -----------------------------------------------------
        rows, errors, meta = parse_gstr2b_json(json.dumps(sample_doc).encode("utf-8"))
        fetch = models.GSTR2BFetch(
            firm_id=firm.id, client_gstin_id=gstin.id, period=PERIOD, version=1,
            is_current=True, source=models.FetchSource.SAMPLE,
            raw_payload=sample_doc, fetched_by=actor.id,
        )
        db.add(fetch)
        db.flush()
        for row in rows:
            db.add(models.GSTR2BEntry(firm_id=firm.id, fetch_id=fetch.id, **row))
        audit.record(db, firm_id=firm.id, user_id=actor.id,
                     action=AuditAction.FETCH_GSTR2B, entity_type="GSTR2BFetch",
                     entity_id=fetch.id, metadata={"rows": len(rows), "period": PERIOD})
        print(f"Loaded {len(rows)} GSTR-2B entries")
        db.flush()

        # --- reconciliation -----------------------------------------------------
        from app.routers.reconciliation import itc_lapse_deadline

        purchase_entries = db.query(models.PurchaseRegisterEntry).filter_by(
            upload_id=upload.id).all()
        gstr_entries = db.query(models.GSTR2BEntry).filter_by(fetch_id=fetch.id).all()

        tolerances = MatchTolerances()
        results = reconcile(purchase_entries, gstr_entries, tolerances=tolerances)

        run = models.ReconciliationRun(
            firm_id=firm.id, client_gstin_id=gstin.id, period=PERIOD, version=1,
            is_current=True, upload_id=upload.id, fetch_id=fetch.id,
            status=models.RunStatus.COMPLETED, run_by=actor.id,
            tolerances_used=tolerances.as_dict(),
        )
        db.add(run)
        db.flush()

        deadline = itc_lapse_deadline(PERIOD)
        exception_count = 0
        for r in results:
            result = models.ReconciliationResult(
                firm_id=firm.id, run_id=run.id,
                purchase_entry_id=r.purchase_entry_id,
                gstr2b_entry_id=r.gstr2b_entry_id,
                match_status=models.MatchStatus(r.match_status),
                confidence_score=r.confidence_score,
                plain_language_note=r.notes, match_key=r.match_key,
            )
            db.add(result)
            db.flush()

            auto = (r.match_status == "FORMATTING_MISMATCH"
                    and r.confidence_score >= tolerances.auto_resolve_threshold)
            needs = r.match_status in FLAGGED or (
                r.match_status == "FORMATTING_MISMATCH" and not auto
            )
            if needs or auto:
                db.add(models.ReconciliationException(
                    firm_id=firm.id, reconciliation_result_id=result.id,
                    client_gstin_id=gstin.id, period=PERIOD, match_key=r.match_key,
                    status=(models.ExceptionStatus.AUTO_RESOLVED if auto
                            else models.ExceptionStatus.OPEN),
                    itc_lapse_deadline=(deadline if r.match_status in AT_RISK else None),
                ))
                exception_count += 1

        audit.record(db, firm_id=firm.id, user_id=actor.id,
                     action=AuditAction.RECONCILIATION_RUN,
                     entity_type="ReconciliationRun", entity_id=run.id,
                     metadata={"results": len(results), "period": PERIOD})
        db.commit()

        counts: dict[str, int] = {}
        for r in results:
            counts[r.match_status] = counts.get(r.match_status, 0) + 1

        print(f"Reconciliation complete: {len(results)} results, "
              f"{exception_count} exceptions")
        for k in sorted(counts):
            print(f"    {k:24} {counts[k]}")
        print()
        print("Sign in with:")
        for u in staff:
            role = u.role.value if hasattr(u.role, "value") else str(u.role)
            print(f"    {u.email:<26} / {DEMO_PASSWORD}   ({role.title()})")
        if len(staff) == 1:
            print()
            print("    Approving a resolution needs a second person: the app refuses")
            print("    to let anyone sign off their own work. Invite a colleague from")
            print("    the app, or re-seed with SEED_EXTRA_STAFF=1 for a manager and")
            print("    an associate as well.")
        print()
        print(f"Client GSTIN id (for API calls): {gstin.id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
