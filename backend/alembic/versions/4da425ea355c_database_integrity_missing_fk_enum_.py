"""database integrity: missing FK, enum CHECK constraints, append-only audit trigger

Revision ID: 4da425ea355c
Revises: 3a1603e70503
Create Date: 2026-09-21

Three things the code and documentation already claimed, which the database
was not actually enforcing. Found by running the migrations against
PostgreSQL for the first time and diffing the result against the models.

1. users.scoped_client_id -> clients.id was never created. The baseline
   declared it with use_alter=True to break the users<->clients cycle, and
   op.create_table() does not emit the deferred ALTER that create_all() would.
   SQLite does not enforce foreign keys, so nothing noticed.

2. Enum columns were plain VARCHAR. SQLAlchemy 2.0 no longer emits a CHECK
   for a non-native Enum unless asked, so the database would accept
   role = 'SUPERADMIN'. The ORM rejects that on its own write path, but the
   role column drives authorisation and deserves a second line of defence.

3. audit_logs was append-only by convention only. On PostgreSQL a trigger now
   rejects UPDATE, DELETE and TRUNCATE. The hash chain still detects a
   tamperer who removes the trigger, but a stray statement or a careless
   migration can no longer rewrite history by accident.

Adding a new value to any enum below now requires a migration that replaces
its CHECK constraint. That is the price of enforcing it in the database.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4da425ea355c"
down_revision: Union[str, None] = "3a1603e70503"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, enum name, allowed values). Static on purpose: a migration
# must describe the schema as it was, not as the models are today.
ENUM_COLUMNS = [
    ('clients', 'entity_type', 'entity_type',
     ['PROPRIETORSHIP', 'PARTNERSHIP', 'LLP', 'COMPANY', 'TRUST', 'OTHER']),
    ('clients', 'engagement_type', 'engagement_type',
     ['COMPLIANCE_ONLY', 'AUDIT', 'BOTH']),
    ('clients', 'status', 'client_status',
     ['ONBOARDING', 'ACTIVE', 'PAUSED', 'OFFBOARDED']),
    ('consent_logs', 'consent_type', 'consent_type',
     ['GSP_GSTR_ACCESS', 'DATA_PROCESSING']),
    ('exceptions', 'status', 'exception_status',
     ['OPEN', 'IN_PROGRESS', 'PENDING_APPROVAL', 'APPROVED', 'REWORK', 'AUTO_RESOLVED']),
    ('firms', 'subscription_tier', 'subscription_tier',
     ['STARTER', 'GROWTH', 'ENTERPRISE']),
    ('firms', 'subscription_status', 'subscription_status',
     ['TRIALING', 'ACTIVE', 'PAST_DUE', 'CANCELLED']),
    ('gstr2b_fetches', 'source', 'fetch_source',
     ['API', 'MANUAL', 'SAMPLE']),
    ('invitations', 'role', 'invite_role',
     ['OWNER', 'PARTNER', 'MANAGER', 'ASSOCIATE', 'CLIENT_VIEWER']),
    ('notices', 'status', 'notice_status',
     ['RECEIVED', 'UNDER_REVIEW', 'RESPONSE_DRAFTED', 'PARTNER_APPROVED', 'FILED', 'CLOSED']),
    ('purchase_register_uploads', 'status', 'upload_status',
     ['PENDING', 'VALIDATED', 'IMPORTED', 'REJECTED']),
    ('reconciliation_results', 'match_status', 'match_status',
     ['MATCHED', 'FORMATTING_MISMATCH', 'SUPPLIER_NOT_FILED', 'VALUE_MISMATCH', 'CREDIT_NOTE_DISCREPANCY', 'MISSING_IN_BOOKS']),
    ('reconciliation_runs', 'status', 'run_status',
     ['RUNNING', 'COMPLETED', 'FAILED']),
    ('subscriptions', 'tier', 'sub_tier',
     ['STARTER', 'GROWTH', 'ENTERPRISE']),
    ('subscriptions', 'status', 'sub_status',
     ['TRIALING', 'ACTIVE', 'PAST_DUE', 'CANCELLED']),
    ('users', 'role', 'user_role',
     ['OWNER', 'PARTNER', 'MANAGER', 'ASSOCIATE', 'CLIENT_VIEWER']),
]


FK_NAME = "fk_users_scoped_client_id_clients"

TRIGGER_FUNCTION_NAME = "audit_logs_reject_mutation"
INSTALL_AUDIT_TRIGGER_SQL = [
    f"""
    CREATE OR REPLACE FUNCTION {TRIGGER_FUNCTION_NAME}() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_logs is append-only: % is not permitted', TG_OP
            USING ERRCODE = 'integrity_constraint_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    f"""
    CREATE TRIGGER audit_logs_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_logs
        FOR EACH ROW EXECUTE FUNCTION {TRIGGER_FUNCTION_NAME}()
    """,
    f"""
    CREATE TRIGGER audit_logs_no_truncate
        BEFORE TRUNCATE ON audit_logs
        FOR EACH STATEMENT EXECUTE FUNCTION {TRIGGER_FUNCTION_NAME}()
    """,
]
UNINSTALL_AUDIT_TRIGGER_SQL = [
    "DROP TRIGGER IF EXISTS audit_logs_no_truncate ON audit_logs",
    "DROP TRIGGER IF EXISTS audit_logs_no_update_delete ON audit_logs",
    f"DROP FUNCTION IF EXISTS {TRIGGER_FUNCTION_NAME}()",
]


def _ck_name(table: str, enum_name: str) -> str:
    """The name to PASS to Alembic, which is deliberately just the enum name.

    The naming convention in app/database.py is `ck_<table>_<name>`, and it is
    applied to whatever name is supplied. Passing an already-prefixed name
    therefore doubles it (`ck_users_ck_users_user_role`), which then differs
    from what create_all() builds (`ck_users_user_role`) and breaks any later
    migration that replaces a constraint by name.
    """
    return enum_name


def _ck_actual_name(table: str, enum_name: str) -> str:
    """The name the constraint really has in the database, after the convention."""
    return f"ck_{table}_{enum_name}"


def _ck_sql(column: str, values: list) -> str:
    quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({quoted})"


def _foreign_key_exists(bind) -> bool:
    insp = sa.inspect(bind)
    return any(
        fk.get("referred_table") == "clients"
        and fk.get("constrained_columns") == ["scoped_client_id"]
        for fk in insp.get_foreign_keys("users")
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # 1. The missing foreign key. Guarded, because a database built with
    #    create_all() (the seed script, the test suite) already has it.
    if not _foreign_key_exists(bind):
        with op.batch_alter_table("users") as batch_op:
            batch_op.create_foreign_key(FK_NAME, "clients", ["scoped_client_id"], ["id"])

    # 2. CHECK constraints on every enum-backed column, one table rebuild per
    #    table on SQLite (which cannot ALTER in place) and plain ALTERs on
    #    PostgreSQL.
    by_table: dict[str, list] = {}
    for table, column, enum_name, values in ENUM_COLUMNS:
        by_table.setdefault(table, []).append((column, enum_name, values))

    insp = sa.inspect(bind)
    for table, cols in by_table.items():
        existing = {c["name"] for c in insp.get_check_constraints(table)}
        todo = [(c, n, v) for c, n, v in cols if _ck_actual_name(table, n) not in existing]
        if not todo:
            continue
        with op.batch_alter_table(table) as batch_op:
            for column, enum_name, values in todo:
                batch_op.create_check_constraint(
                    _ck_name(table, enum_name), _ck_sql(column, values)
                )

    # 3. Append-only audit log. PostgreSQL only: SQLite has no equivalent that
    #    is worth the complexity, and relies on the hash chain alone.
    if is_postgres:
        for statement in INSTALL_AUDIT_TRIGGER_SQL:
            op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for statement in UNINSTALL_AUDIT_TRIGGER_SQL:
            op.execute(statement)

    by_table: dict[str, list] = {}
    for table, column, enum_name, values in ENUM_COLUMNS:
        by_table.setdefault(table, []).append(enum_name)
    for table, enum_names in by_table.items():
        with op.batch_alter_table(table) as batch_op:
            for enum_name in enum_names:
                batch_op.drop_constraint(_ck_name(table, enum_name), type_="check")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint(FK_NAME, type_="foreignkey")
