"""notice management: encrypted file metadata, extraction and analysis state, approval trail

Revision ID: 9c5e2f7a1b30
Revises: 182eb8deeead
Create Date: 2026-09-22

Extends `notices`, which the baseline created as a bare shell, into the table
the notice workflow needs. Every change is an added column or constraint, so
existing rows are untouched.

The three NOT NULL columns get a server default for the rows that already
exist, which is dropped again afterwards: the application supplies the value
on every insert, and a lingering database default would only hide a code path
that forgot to.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9c5e2f7a1b30"
down_revision: Union[str, None] = "182eb8deeead"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (column, enum name, allowed values, default for existing rows)
NEW_ENUMS = [
    ("extraction_status", "notice_extraction_status",
     ["PENDING", "DONE", "NEEDS_OCR", "FAILED", "MANUAL"], "PENDING"),
    ("analysis_status", "notice_analysis_status",
     ["NOT_RUN", "DONE", "FAILED"], "NOT_RUN"),
]

# Columns referencing users(id). Constraint names follow the naming
# convention in app/database.py: fk_<table>_<column>_<referred table>.
USER_FK_COLUMNS = ["drafted_by", "submitted_by", "approved_by", "filed_by", "closed_by"]


def _ck_sql(column: str, values: list) -> str:
    quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    with op.batch_alter_table("notices") as batch:
        batch.add_column(sa.Column("section", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("period_to", sa.String(length=7), nullable=True))
        batch.add_column(sa.Column("deadline_confirmed", sa.Boolean(), nullable=False,
                                   server_default=sa.false()))

        batch.add_column(sa.Column("content_type", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("size_bytes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("file_sha256", sa.String(length=64), nullable=True))

        for column, _enum_name, _values, default in NEW_ENUMS:
            batch.add_column(sa.Column(column, sa.String(length=40), nullable=False,
                                       server_default=default))
        batch.add_column(sa.Column("extraction_method", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("extraction_message", sa.Text(), nullable=True))
        batch.add_column(sa.Column("page_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("analysis_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("analysed_at", sa.DateTime(), nullable=True))

        batch.add_column(sa.Column("drafted_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("drafted_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("submitted_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("submitted_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("rework_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("approved_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("approved_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("filed_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("filed_at", sa.Date(), nullable=True))
        batch.add_column(sa.Column("filing_reference", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("closed_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("closed_at", sa.DateTime(), nullable=True))

        for column in USER_FK_COLUMNS:
            batch.create_foreign_key(
                op.f(f"fk_notices_{column}_users"), "users", [column], ["id"],
            )
        for column, enum_name, values, _default in NEW_ENUMS:
            # Pass the bare enum name: the naming convention adds the
            # ck_<table>_ prefix itself (see migration 4da425ea355c).
            batch.create_check_constraint(enum_name, _ck_sql(column, values))

    with op.batch_alter_table("notices") as batch:
        batch.alter_column("deadline_confirmed", server_default=None)
        for column, _enum_name, _values, _default in NEW_ENUMS:
            batch.alter_column(column, server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("notices") as batch:
        for _column, enum_name, _values, _default in NEW_ENUMS:
            batch.drop_constraint(enum_name, type_="check")
        for column in USER_FK_COLUMNS:
            batch.drop_constraint(op.f(f"fk_notices_{column}_users"), type_="foreignkey")

        for column in [
            "closed_at", "closed_by", "filing_reference", "filed_at", "filed_by",
            "approved_at", "approved_by", "rework_reason", "submitted_at",
            "submitted_by", "drafted_at", "drafted_by", "analysed_at",
            "analysis_error", "page_count", "extraction_message", "extraction_method",
            "analysis_status", "extraction_status", "file_sha256", "size_bytes",
            "content_type", "deadline_confirmed", "period_to", "section",
        ]:
            batch.drop_column(column)
