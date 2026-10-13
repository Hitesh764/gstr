"""mapped ratio analysis with reviewer and engagement-partner approval

Revision ID: a4d8f19c2e60
Revises: b7c1e20f94a6
"""
from alembic import op
import sqlalchemy as sa

revision = "a4d8f19c2e60"
down_revision = "b7c1e20f94a6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_ratio_analyses",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("DRAFT", "MANAGER_REVIEWED", "APPROVED", name="audit_ratio_analysis_status", native_enum=False, length=40), nullable=False),
        sa.Column("closing_upload_id", sa.String(), nullable=False),
        sa.Column("opening_upload_id", sa.String(), nullable=True),
        sa.Column("prior_opening_source_reference", sa.String(length=500), nullable=True),
        sa.Column("account_mapping", sa.JSON(), nullable=False),
        sa.Column("ratio_results", sa.JSON(), nullable=False),
        sa.Column("movement_threshold_pct", sa.Numeric(7, 2), nullable=False),
        sa.Column("risk_flag_ids", sa.JSON(), nullable=False),
        sa.Column("prepared_by", sa.String(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["closing_upload_id"], ["trial_balance_uploads.id"]),
        sa.ForeignKeyConstraint(["opening_upload_id"], ["trial_balance_uploads.id"]),
        sa.ForeignKeyConstraint(["prepared_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "version", name="uq_audit_ratio_analysis_engagement_version"),
    )
    op.create_index("ix_audit_ratio_analyses_firm_id", "audit_ratio_analyses", ["firm_id"])
    op.create_index("ix_audit_ratio_analyses_engagement_id", "audit_ratio_analyses", ["engagement_id"])
    op.create_index("ix_audit_ratio_analysis_engagement", "audit_ratio_analyses", ["firm_id", "engagement_id", "version"])


def downgrade():
    op.drop_index("ix_audit_ratio_analysis_engagement", table_name="audit_ratio_analyses")
    op.drop_index("ix_audit_ratio_analyses_engagement_id", table_name="audit_ratio_analyses")
    op.drop_index("ix_audit_ratio_analyses_firm_id", table_name="audit_ratio_analyses")
    op.drop_table("audit_ratio_analyses")
