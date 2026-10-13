"""system flagged trial balance movements

Revision ID: d3e7a19c54b2
Revises: c91d4e6a20f3
"""
from alembic import op
import sqlalchemy as sa

revision = "d3e7a19c54b2"
down_revision = "c91d4e6a20f3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "risk_flags",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("trial_balance_line_id", sa.String(), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("flag_type", sa.Enum("VARIANCE", "RATIO", "RELATED_PARTY", "STATUTORY_DUES", "OTHER",
                                        name="risk_flag_type", native_enum=False, length=40), nullable=False),
        sa.Column("severity", sa.String(length=40), nullable=False),
        sa.Column("system_note", sa.Text(), nullable=False),
        sa.Column("trigger_data", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("SYSTEM_FLAGGED", "UNDER_REVIEW", "RESOLVED", "DISMISSED",
                                     name="risk_flag_status", native_enum=False, length=40), nullable=False),
        sa.Column("assigned_to", sa.String(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["trial_balance_line_id"], ["trial_balance_lines.id"]),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "trial_balance_line_id", "flag_type", "policy_version",
                            name="uq_risk_flag_line_type_policy_version"),
    )
    op.create_index("ix_risk_flags_firm_id", "risk_flags", ["firm_id"])
    op.create_index("ix_risk_flags_engagement_id", "risk_flags", ["engagement_id"])
    op.create_index("ix_risk_flag_engagement_status", "risk_flags", ["firm_id", "engagement_id", "status"])
    op.create_index("ix_risk_flag_trial_balance_line", "risk_flags", ["trial_balance_line_id"])


def downgrade():
    op.drop_index("ix_risk_flag_trial_balance_line", table_name="risk_flags")
    op.drop_index("ix_risk_flag_engagement_status", table_name="risk_flags")
    op.drop_index("ix_risk_flags_engagement_id", table_name="risk_flags")
    op.drop_index("ix_risk_flags_firm_id", table_name="risk_flags")
    op.drop_table("risk_flags")
