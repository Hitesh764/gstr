"""dated open-item ledger and generated debtor/creditor ageing schedules

Revision ID: e2f9a641c5b8
Revises: f6a4c2e1b790
"""
from alembic import op
import sqlalchemy as sa

revision = "e2f9a641c5b8"
down_revision = "f6a4c2e1b790"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_ledger_uploads",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("original_filename", sa.String(length=400), nullable=False),
        sa.Column("file_path", sa.String(length=1000), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "version", name="uq_audit_ledger_engagement_version"),
    )
    op.create_index("ix_audit_ledger_uploads_firm_id", "audit_ledger_uploads", ["firm_id"])
    op.create_index("ix_audit_ledger_uploads_engagement_id", "audit_ledger_uploads", ["engagement_id"])
    op.create_index("ix_audit_ledger_current", "audit_ledger_uploads", ["engagement_id", "is_current"])
    op.create_table(
        "audit_ledger_lines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("upload_id", sa.String(), nullable=False),
        sa.Column("account_type", sa.Enum("DEBTORS", "CREDITORS", name="audit_ageing_type", native_enum=False, length=40), nullable=False),
        sa.Column("account_code", sa.String(length=100), nullable=True),
        sa.Column("account_name", sa.String(length=300), nullable=True),
        sa.Column("party_name", sa.String(length=300), nullable=False),
        sa.Column("invoice_number", sa.String(length=120), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("open_balance", sa.Numeric(18, 2), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["upload_id"], ["audit_ledger_uploads.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_ledger_lines_firm_id", "audit_ledger_lines", ["firm_id"])
    op.create_index("ix_audit_ledger_lines_upload_id", "audit_ledger_lines", ["upload_id"])
    op.create_index("ix_audit_ledger_line_source_type", "audit_ledger_lines", ["firm_id", "upload_id", "account_type"])
    op.create_table(
        "audit_ageing_schedules",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("schedule_type", sa.Enum("DEBTORS", "CREDITORS", name="audit_ageing_schedule_type", native_enum=False, length=40), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("source_upload_id", sa.String(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("generated_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["source_upload_id"], ["audit_ledger_uploads.id"]),
        sa.ForeignKeyConstraint(["generated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_ageing_schedules_firm_id", "audit_ageing_schedules", ["firm_id"])
    op.create_index("ix_audit_ageing_schedules_engagement_id", "audit_ageing_schedules", ["engagement_id"])
    op.create_index("ix_audit_ageing_engagement", "audit_ageing_schedules", ["firm_id", "engagement_id", "generated_at"])


def downgrade():
    op.drop_index("ix_audit_ageing_engagement", table_name="audit_ageing_schedules")
    op.drop_index("ix_audit_ageing_schedules_engagement_id", table_name="audit_ageing_schedules")
    op.drop_index("ix_audit_ageing_schedules_firm_id", table_name="audit_ageing_schedules")
    op.drop_table("audit_ageing_schedules")
    op.drop_index("ix_audit_ledger_line_source_type", table_name="audit_ledger_lines")
    op.drop_index("ix_audit_ledger_lines_upload_id", table_name="audit_ledger_lines")
    op.drop_index("ix_audit_ledger_lines_firm_id", table_name="audit_ledger_lines")
    op.drop_table("audit_ledger_lines")
    op.drop_index("ix_audit_ledger_current", table_name="audit_ledger_uploads")
    op.drop_index("ix_audit_ledger_uploads_engagement_id", table_name="audit_ledger_uploads")
    op.drop_index("ix_audit_ledger_uploads_firm_id", table_name="audit_ledger_uploads")
    op.drop_table("audit_ledger_uploads")
