"""versioned CARO draft, reviewer and partner-finalization workflow

Revision ID: d2c84a7b61e9
Revises: c7f1d9a2e540
"""
from alembic import op
import sqlalchemy as sa

revision = "d2c84a7b61e9"
down_revision = "c7f1d9a2e540"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "caro_clause_responses",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("clause_definition_id", sa.String(), nullable=False),
        sa.Column("clause_number", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("applicability", sa.String(length=30), nullable=False),
        sa.Column("applicability_reasoning", sa.Text(), nullable=False),
        sa.Column("draft_narrative", sa.Text(), nullable=False),
        sa.Column("final_narrative", sa.Text(), nullable=True),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("DRAFT", "MANAGER_REVIEWED", "FINALIZED", "SUPERSEDED",
                                      name="caro_response_status", native_enum=False, length=40), nullable=False),
        sa.Column("prepared_by", sa.String(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("finalized_by", sa.String(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["clause_definition_id"], ["caro_clause_definitions.id"]),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["finalized_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["prepared_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "clause_number", "version",
                            name="uq_caro_response_eng_clause_version"),
    )
    op.create_index("ix_caro_clause_responses_firm_id", "caro_clause_responses", ["firm_id"])
    op.create_index("ix_caro_clause_responses_engagement_id", "caro_clause_responses", ["engagement_id"])
    op.create_index("ix_caro_response_eng_status", "caro_clause_responses", ["firm_id", "engagement_id", "status"])


def downgrade():
    op.drop_index("ix_caro_response_eng_status", table_name="caro_clause_responses")
    op.drop_index("ix_caro_clause_responses_engagement_id", table_name="caro_clause_responses")
    op.drop_index("ix_caro_clause_responses_firm_id", table_name="caro_clause_responses")
    op.drop_table("caro_clause_responses")
