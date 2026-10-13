"""
Database-level guarantees, verified on a database built by the real migrations.

The rest of the suite builds its schema with `create_all()`, which is fast but
cannot see defects in the migrations themselves. Three such defects reached
this codebase unnoticed: a foreign key that `op.create_table` silently
skipped, enum columns with no CHECK constraint, and an append-only audit log
that was only append-only by convention. Each was invisible on SQLite, which
neither enforces foreign keys nor supports the trigger.

So this module creates a scratch database, runs `alembic upgrade head` into it
in a subprocess, and asserts the properties directly with raw SQL. It runs
only when TEST_DATABASE_URL points at PostgreSQL, and skips otherwise.
"""
import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL.startswith("postgresql"),
    reason="needs PostgreSQL: set TEST_DATABASE_URL (see tests/conftest.py)",
)

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    """Run alembic in a subprocess against `database_url`.

    A subprocess, because app.config reads DATABASE_URL once at import time;
    changing it in this process would have no effect on Alembic's env.py.
    """
    env = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR, env=env, capture_output=True, text=True, timeout=300,
    )


class _ScratchDatabase:
    """A throwaway database on the same server as the test database."""

    def __init__(self):
        base = make_url(TEST_DATABASE_URL)
        self.name = f"{base.database}_mig_{uuid.uuid4().hex[:8]}"
        self.url = base.set(database=self.name).render_as_string(hide_password=False)
        self._admin_url = TEST_DATABASE_URL

    def create(self):
        admin = create_engine(self._admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{self.name}"'))
        admin.dispose()
        return self

    def drop(self):
        admin = create_engine(self._admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def migrated():
    """A database built by `alembic upgrade head`, shared by the tests below."""
    scratch = _ScratchDatabase().create()
    try:
        result = _alembic(scratch.url, "upgrade", "head")
        assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr[-2000:]}"
        engine = create_engine(scratch.url)
        yield scratch, engine
        engine.dispose()
    finally:
        scratch.drop()


FIRM_SQL = text(
    "INSERT INTO firms (id, name, contact_email, subscription_tier, "
    "subscription_status, require_2fa_for_partners, created_at, updated_at) "
    "VALUES (:id, 'F', 'f@f.in', 'STARTER', 'TRIALING', false, now(), now())"
)
USER_SQL = text(
    "INSERT INTO users (id, firm_id, name, email, role, password_hash, "
    "totp_enabled, is_active, email_verified, phone_verified, "
    "failed_login_attempts, scoped_client_id, created_at, updated_at) "
    "VALUES (:id, :firm, 'U', :email, :role, 'h', false, true, false, false, 0, "
    ":scoped, now(), now())"
)
AUDIT_SQL = text(
    "INSERT INTO audit_logs (id, firm_id, action, created_at) "
    "VALUES (:id, :firm, 'TEST', now())"
)


def _seed_firm(conn, firm_id=None):
    firm_id = firm_id or uuid.uuid4().hex
    conn.execute(FIRM_SQL, {"id": firm_id})
    return firm_id


# ---------------------------------------------------------------------------
# Schema fidelity
# ---------------------------------------------------------------------------

def test_migrations_leave_no_drift_from_the_models(migrated):
    scratch, _ = migrated
    result = _alembic(scratch.url, "check")
    assert result.returncode == 0, (
        "The migrations do not produce the schema the models describe:\n"
        + (result.stdout + result.stderr)[-2000:]
    )


def test_constraint_names_match_create_all(migrated, engine):
    """A database built by migrations and one built by create_all must name
    their CHECK constraints identically, or a later migration that replaces a
    constraint by name works on one and fails on the other. This is the test
    that catches a doubled naming-convention prefix."""
    _, mig_engine = migrated
    query = text(
        "SELECT conname FROM pg_constraint WHERE contype = 'c' "
        "AND connamespace = 'public'::regnamespace ORDER BY 1"
    )
    with mig_engine.connect() as c1, engine.connect() as c2:
        from_migrations = {r[0] for r in c1.execute(query)}
        from_create_all = {r[0] for r in c2.execute(query)}
    assert from_migrations == from_create_all, (
        f"only in migrations: {sorted(from_migrations - from_create_all)}; "
        f"only in create_all: {sorted(from_create_all - from_migrations)}"
    )
    assert len(from_migrations) >= 16, "expected a CHECK on every enum column"


# ---------------------------------------------------------------------------
# Enforced constraints
# ---------------------------------------------------------------------------

def test_an_invalid_role_is_rejected_by_the_database(migrated):
    _, engine = migrated
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            firm = _seed_firm(conn)
            conn.execute(USER_SQL, {"id": "u1", "firm": firm, "email": "a@a.in",
                                    "role": "SUPERADMIN", "scoped": None})
    assert "ck_users_user_role" in str(exc.value)


def test_a_valid_role_is_accepted(migrated):
    _, engine = migrated
    with engine.connect() as conn:
        trans = conn.begin()
        firm = _seed_firm(conn)
        conn.execute(USER_SQL, {"id": "u-ok", "firm": firm, "email": "ok@a.in",
                                "role": "OWNER", "scoped": None})
        trans.rollback()


def test_scoped_client_foreign_key_is_enforced(migrated):
    """users.scoped_client_id -> clients.id was silently never created."""
    _, engine = migrated
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            firm = _seed_firm(conn)
            conn.execute(USER_SQL, {"id": "u2", "firm": firm, "email": "b@a.in",
                                    "role": "CLIENT_VIEWER", "scoped": "no-such-client"})
    assert "fk_users_scoped_client_id_clients" in str(exc.value)


# ---------------------------------------------------------------------------
# Append-only audit log
# ---------------------------------------------------------------------------

def _committed_audit_row(engine) -> tuple[str, str]:
    """Insert one audit row and commit it, so mutation attempts have a target."""
    with engine.begin() as conn:
        firm = _seed_firm(conn)
        audit_id = uuid.uuid4().hex
        conn.execute(AUDIT_SQL, {"id": audit_id, "firm": firm})
    return firm, audit_id


def test_audit_log_accepts_new_entries(migrated):
    _, engine = migrated
    _committed_audit_row(engine)   # would raise if appending were blocked


def test_audit_log_rejects_update(migrated):
    _, engine = migrated
    _, audit_id = _committed_audit_row(engine)
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            conn.execute(text("UPDATE audit_logs SET action = 'REWRITTEN' WHERE id = :i"),
                         {"i": audit_id})
    assert "append-only" in str(exc.value)

    with engine.connect() as conn:
        action = conn.execute(text("SELECT action FROM audit_logs WHERE id = :i"),
                              {"i": audit_id}).scalar()
    assert action == "TEST", "the failed UPDATE must not have changed the row"


def test_audit_log_rejects_delete(migrated):
    _, engine = migrated
    _, audit_id = _committed_audit_row(engine)
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM audit_logs WHERE id = :i"), {"i": audit_id})
    assert "append-only" in str(exc.value)


def test_audit_log_rejects_truncate(migrated):
    _, engine = migrated
    _committed_audit_row(engine)
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE audit_logs"))
    assert "append-only" in str(exc.value)


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------

def test_full_migration_round_trip():
    """Every revision must be reversible, and re-applying must land on the
    same schema. A downgrade that leaves debris breaks the next upgrade."""
    scratch = _ScratchDatabase().create()
    try:
        for args in (("upgrade", "head"), ("downgrade", "base"),
                     ("upgrade", "head"), ("check",)):
            result = _alembic(scratch.url, *args)
            assert result.returncode == 0, (
                f"`alembic {' '.join(args)}` failed:\n"
                f"{(result.stdout + result.stderr)[-2000:]}"
            )
    finally:
        scratch.drop()


# ---------------------------------------------------------------------------
# Notices (migration 9c5e2f7a1b30)
# ---------------------------------------------------------------------------

NOTICE_SQL = text(
    "INSERT INTO notices (id, firm_id, client_id, status, extraction_status, "
    "analysis_status, deadline_confirmed, approved_by, created_at, updated_at) "
    "VALUES (:id, :firm, :client, 'RECEIVED', :ext, :ana, false, :approved, now(), now())"
)


def _seed_client(conn, firm_id) -> str:
    from app import models

    client_id = uuid.uuid4().hex
    conn.execute(models.Client.__table__.insert().values(
        id=client_id, firm_id=firm_id, name="Notice Client"))
    return client_id


def _notice(conn, firm, client, *, ext="PENDING", ana="NOT_RUN", approved=None):
    conn.execute(NOTICE_SQL, {"id": uuid.uuid4().hex, "firm": firm, "client": client,
                              "ext": ext, "ana": ana, "approved": approved})


def test_a_valid_notice_is_accepted(migrated):
    _, engine = migrated
    with engine.connect() as conn:
        trans = conn.begin()
        firm = _seed_firm(conn)
        _notice(conn, firm, _seed_client(conn, firm), ext="NEEDS_OCR", ana="FAILED")
        trans.rollback()


def test_an_invalid_extraction_status_is_rejected_by_the_database(migrated):
    _, engine = migrated
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            firm = _seed_firm(conn)
            _notice(conn, firm, _seed_client(conn, firm), ext="MAYBE")
    assert "ck_notices_notice_extraction_status" in str(exc.value)


def test_an_invalid_analysis_status_is_rejected_by_the_database(migrated):
    _, engine = migrated
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            firm = _seed_firm(conn)
            _notice(conn, firm, _seed_client(conn, firm), ana="SORT_OF")
    assert "ck_notices_notice_analysis_status" in str(exc.value)


def test_the_approver_must_be_a_real_user(migrated):
    """The approval trail columns reference users, so a notice cannot record an
    approval by someone who does not exist."""
    _, engine = migrated
    with pytest.raises(DBAPIError) as exc:
        with engine.begin() as conn:
            firm = _seed_firm(conn)
            _notice(conn, firm, _seed_client(conn, firm), approved="no-such-user")
    assert "fk_notices_approved_by_users" in str(exc.value)


def test_the_notice_migration_can_be_reversed_and_reapplied(migrated):
    """Downgrading one step removes exactly what the migration added, and
    upgrading again restores it, with no drift from the models either way."""
    scratch, _ = migrated
    down = _alembic(scratch.url, "downgrade", "182eb8deeead")
    assert down.returncode == 0, down.stderr[-2000:]
    up = _alembic(scratch.url, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-2000:]
    check = _alembic(scratch.url, "check")
    assert check.returncode == 0, (check.stdout + check.stderr)[-2000:]
