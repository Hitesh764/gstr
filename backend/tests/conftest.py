"""
Shared pytest fixtures.

By default the suite runs against a throwaway SQLite file. Set
TEST_DATABASE_URL to run it against PostgreSQL instead, which is what
production uses and the only place foreign keys, CHECK constraints and the
audit-log trigger are actually enforced:

    TEST_DATABASE_URL=postgresql+psycopg://gstr2b:<pw>@127.0.0.1:5433/gstr2b_test pytest

The suite EMPTIES every table before each test, so it refuses to run against
any database whose name does not contain "test". That guard exists because
the alternative is one mistyped URL away from erasing real work.

In both cases the `get_db` dependency is overridden so the application uses
the test database too.

The `two_firms` fixture builds the scenario the isolation tests need: two
separate firms, each with its own users, client, GSTIN and reconciled data.
Everything in firm B must be invisible to firm A.
"""
import os
import sys
import tempfile
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import models  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.security import hash_password  # noqa: E402

TEST_PASSWORD = "test-password-123"


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()


def _is_postgres(engine) -> bool:
    return engine.dialect.name == "postgresql"


def _assert_safe_test_database(url: str) -> None:
    from sqlalchemy.engine import make_url

    name = (make_url(url).database or "").lower()
    if "test" not in name:
        raise RuntimeError(
            f"Refusing to run the test suite against database {name!r}: the name "
            f"must contain 'test'. The suite deletes every row in every table."
        )


@pytest.fixture(scope="session")
def engine():
    if TEST_DATABASE_URL:
        _assert_safe_test_database(TEST_DATABASE_URL)
        eng = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
        if _is_postgres(eng):
            # A clean slate every session, so a schema left over from an
            # older version of the models cannot mask a real failure.
            with eng.begin() as conn:
                conn.execute(text("DROP SCHEMA public CASCADE"))
                conn.execute(text("CREATE SCHEMA public"))
        else:
            Base.metadata.drop_all(bind=eng)
        Base.metadata.create_all(bind=eng)
        yield eng
        eng.dispose()
        return

    fd, path = tempfile.mkstemp(suffix=".db", prefix="gstr2b_test_")
    os.close(fd)
    eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture(scope="session")
def SessionFactory(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def clean_tables(engine):
    """Empty every table before each test.

    The engine is session-scoped for speed, but fixtures commit real rows
    and several columns are globally unique (user email, for one). Without
    a reset between tests the second test to build the same scenario would
    collide on those constraints. Deleting in reverse dependency order
    satisfies the foreign keys.
    """
    with engine.begin() as conn:
        if _is_postgres(engine):
            # Foreign keys are enforced here, and users <-> clients reference
            # each other, so no row-by-row delete order is always safe.
            # TRUNCATE ... CASCADE empties everything in one statement.
            names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        else:
            for table in reversed(Base.metadata.sorted_tables):
                conn.execute(table.delete())
    yield


@pytest.fixture
def db(SessionFactory):
    session = SessionFactory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(SessionFactory):
    """TestClient with the database dependency pointed at the test file."""
    from app.main import app

    def _get_test_db():
        session = SessionFactory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _get_test_db
    # The limiter counts per client address across the whole run, and every
    # test comes from the same one. Without a reset, a long enough suite starts
    # failing logins for reasons that have nothing to do with the test.
    app.state.limiter.reset()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------

def _build_firm(db, *, slug: str, firm_name: str, client_name: str,
                gstin_value: str, vendor_gstin: str, period: str = "2026-03"):
    """Create one complete firm with data, for isolation testing."""
    firm = models.Firm(name=firm_name, contact_email=f"owner@{slug}-ca.in")
    db.add(firm)
    db.flush()

    pw = hash_password(TEST_PASSWORD)
    users = {}
    for role, label in [
        (models.UserRole.OWNER, "owner"),
        (models.UserRole.MANAGER, "manager"),
        (models.UserRole.ASSOCIATE, "associate"),
    ]:
        u = models.User(firm_id=firm.id, name=f"{label.title()} {slug}",
                        email=f"{label}@{slug}-ca.in", role=role,
                        password_hash=pw, email_verified=True)
        db.add(u)
        db.flush()
        users[label] = u

    cl = models.Client(firm_id=firm.id, name=client_name,
                       status=models.ClientStatus.ACTIVE)
    db.add(cl)
    db.flush()

    gstin = models.ClientGSTIN(firm_id=firm.id, client_id=cl.id, gstin=gstin_value,
                               state_code=gstin_value[:2], is_primary=True)
    db.add(gstin)
    db.flush()

    upload = models.PurchaseRegisterUpload(
        firm_id=firm.id, client_gstin_id=gstin.id, period=period, version=1,
        is_current=True, status=models.UploadStatus.IMPORTED, row_count=1,
        uploaded_by=users["associate"].id,
    )
    db.add(upload)
    db.flush()

    entry = models.PurchaseRegisterEntry(
        firm_id=firm.id, upload_id=upload.id, vendor_name=f"Vendor {slug}",
        vendor_gstin=vendor_gstin, invoice_number=f"INV-{slug}-001",
        invoice_date=date(2026, 3, 10), taxable_value=Decimal("10000.00"),
        cgst=Decimal("900.00"), sgst=Decimal("900.00"), igst=Decimal("0.00"),
        total_value=Decimal("11800.00"),
    )
    db.add(entry)
    db.flush()

    run = models.ReconciliationRun(
        firm_id=firm.id, client_gstin_id=gstin.id, period=period, version=1,
        is_current=True, upload_id=upload.id,
        status=models.RunStatus.COMPLETED, run_by=users["associate"].id,
    )
    db.add(run)
    db.flush()

    result = models.ReconciliationResult(
        firm_id=firm.id, run_id=run.id, purchase_entry_id=entry.id,
        match_status=models.MatchStatus.SUPPLIER_NOT_FILED, confidence_score=0,
        plain_language_note=f"Secret note belonging to {firm_name}",
        match_key=f"key-{slug}",
    )
    db.add(result)
    db.flush()

    exc = models.ReconciliationException(
        firm_id=firm.id, reconciliation_result_id=result.id,
        client_gstin_id=gstin.id, period=period, match_key=f"key-{slug}",
        status=models.ExceptionStatus.OPEN,
    )
    db.add(exc)
    db.flush()

    return {
        "firm": firm, "users": users, "client": cl, "gstin": gstin,
        "upload": upload, "entry": entry, "run": run,
        "result": result, "exception": exc, "period": period,
    }


@pytest.fixture
def two_firms(db):
    """Two fully-populated, completely separate firms."""
    a = _build_firm(db, slug="alpha", firm_name="Alpha & Co",
                    client_name="Alpha Client Pvt Ltd",
                    gstin_value="27AAPFU0939F1ZV", vendor_gstin="29AAGCB7383J1Z4")
    b = _build_firm(db, slug="bravo", firm_name="Bravo Associates",
                    client_name="Bravo Client Pvt Ltd",
                    gstin_value="29AAGCB7383J1Z4", vendor_gstin="27AAPFU0939F1ZV")
    db.commit()
    return {"a": a, "b": b}


@pytest.fixture
def login(client):
    """Log in and return the Authorization header for that user."""
    def _login(email: str, password: str = TEST_PASSWORD) -> dict:
        resp = client.post("/api/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 200, f"login failed for {email}: {resp.text}"
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}
    return _login


@pytest.fixture(autouse=True)
def isolated_file_storage(tmp_path, monkeypatch):
    """Every test writes uploaded files to its own temporary directory, so the
    suite never touches backend/data/files and tests cannot see each other's files."""
    from app.config import settings
    from app.storage import get_storage

    monkeypatch.setattr(settings, "storage_local_dir", str(tmp_path / "files"))
    get_storage.cache_clear()
    yield
    get_storage.cache_clear()
