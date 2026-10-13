"""versioned trial-balance uploads and account lines

Revision ID: b24e68f1930a
Revises: a81c5d2e739b
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b24e68f1930a"
down_revision: Union[str, None] = "a81c5d2e739b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trial_balance_uploads",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column(
            "period_type",
            sa.Enum("OPENING", "CLOSING", "COMPARATIVE",
                    name="trial_balance_period_type", native_enum=False,
                    length=40, create_constraint=True),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("compares_to_upload_id", sa.String(), nullable=True),
        sa.Column("original_filename", sa.String(length=400), nullable=False),
        sa.Column("file_path", sa.String(length=1000), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["compares_to_upload_id"], ["trial_balance_uploads.id"],
                                name=op.f("fk_trial_balance_uploads_compares_to_upload_id_trial_balance_uploads")),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"],
                                name=op.f("fk_trial_balance_uploads_engagement_id_audit_engagements")),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"],
                                name=op.f("fk_trial_balance_uploads_firm_id_firms")),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"],
                                name=op.f("fk_trial_balance_uploads_uploaded_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trial_balance_uploads")),
        sa.UniqueConstraint("engagement_id", "period_type", "version",
                            name="uq_trial_balance_engagement_type_version"),
    )
    op.create_index("ix_trial_balance_current", "trial_balance_uploads",
                    ["engagement_id", "period_type", "is_current"], unique=False)
    op.create_index(op.f("ix_trial_balance_uploads_firm_id"), "trial_balance_uploads",
                    ["firm_id"], unique=False)
    op.create_index(op.f("ix_trial_balance_uploads_engagement_id"), "trial_balance_uploads",
                    ["engagement_id"], unique=False)

    op.create_table(
        "trial_balance_lines",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("upload_id", sa.String(), nullable=False),
        sa.Column("account_code", sa.String(length=100), nullable=False),
        sa.Column("account_name", sa.String(length=300), nullable=False),
        sa.Column("account_group", sa.String(length=120), nullable=True),
        sa.Column("current_year_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("prior_year_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("variance_amount", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("variance_pct", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("source_row_number", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"],
                                name=op.f("fk_trial_balance_lines_firm_id_firms")),
        sa.ForeignKeyConstraint(["upload_id"], ["trial_balance_uploads.id"],
                                name=op.f("fk_trial_balance_lines_upload_id_trial_balance_uploads")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trial_balance_lines")),
        sa.UniqueConstraint("upload_id", "account_code",
                            name="uq_trial_balance_line_account"),
    )
    op.create_index("ix_trial_balance_line_account", "trial_balance_lines",
                    ["firm_id", "account_code"], unique=False)
    op.create_index(op.f("ix_trial_balance_lines_firm_id"), "trial_balance_lines",
                    ["firm_id"], unique=False)
    op.create_index(op.f("ix_trial_balance_lines_upload_id"), "trial_balance_lines",
                    ["upload_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_trial_balance_lines_upload_id"), table_name="trial_balance_lines")
    op.drop_index(op.f("ix_trial_balance_lines_firm_id"), table_name="trial_balance_lines")
    op.drop_index("ix_trial_balance_line_account", table_name="trial_balance_lines")
    op.drop_table("trial_balance_lines")
    op.drop_index(op.f("ix_trial_balance_uploads_engagement_id"),
                  table_name="trial_balance_uploads")
    op.drop_index(op.f("ix_trial_balance_uploads_firm_id"),
                  table_name="trial_balance_uploads")
    op.drop_index("ix_trial_balance_current", table_name="trial_balance_uploads")
    op.drop_table("trial_balance_uploads")
