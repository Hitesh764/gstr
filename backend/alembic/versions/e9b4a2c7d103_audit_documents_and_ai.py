"""approval-gated audit documents and exact AI generation records

Revision ID: e9b4a2c7d103
Revises: d2c84a7b61e9
"""
from alembic import op
import sqlalchemy as sa

revision = "e9b4a2c7d103"
down_revision = "d2c84a7b61e9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("audit_engagements", sa.Column("reporting_deadline", sa.Date(), nullable=True))
    op.create_table(
        "audit_ai_records",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(length=60), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("exact_prompt", sa.Text(), nullable=False),
        sa.Column("exact_response", sa.Text(), nullable=False),
        sa.Column("response_sha256", sa.String(length=64), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("generated_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["generated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_ai_records_firm_id", "audit_ai_records", ["firm_id"])
    op.create_index("ix_audit_ai_records_engagement_id", "audit_ai_records", ["engagement_id"])
    op.create_index("ix_audit_ai_records_engagement", "audit_ai_records", ["firm_id", "engagement_id", "created_at"])
    op.create_index("ix_audit_ai_records_entity", "audit_ai_records", ["entity_type", "entity_id"])
    op.create_table(
        "management_rep_letters",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("template_version", sa.String(length=80), nullable=False),
        sa.Column("draft_content", sa.Text(), nullable=False),
        sa.Column("final_content", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("DRAFT", "MANAGER_REVIEWED", "PARTNER_APPROVED", "SENT", "RECEIVED", name="management_rep_letter_status", native_enum=False, length=40), nullable=False),
        sa.Column("prepared_by", sa.String(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("sent_to_client_at", sa.DateTime(), nullable=True),
        sa.Column("received_signed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["prepared_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "version", name="uq_mrl_engagement_version"),
    )
    op.create_index("ix_management_rep_letters_firm_id", "management_rep_letters", ["firm_id"])
    op.create_index("ix_management_rep_letters_engagement_id", "management_rep_letters", ["engagement_id"])
    op.create_index("ix_mrl_engagement_status", "management_rep_letters", ["firm_id", "engagement_id", "status"])
    op.create_table(
        "audit_working_papers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("sa_reference", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("cross_references", sa.JSON(), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.Enum("DRAFT", "MANAGER_REVIEWED", "PARTNER_APPROVED", name="working_paper_status", native_enum=False, length=40), nullable=False),
        sa.Column("prepared_by", sa.String(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["prepared_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "version", name="uq_working_paper_engagement_version"),
    )
    op.create_index("ix_audit_working_papers_firm_id", "audit_working_papers", ["firm_id"])
    op.create_index("ix_audit_working_papers_engagement_id", "audit_working_papers", ["engagement_id"])
    op.create_index("ix_working_paper_engagement_status", "audit_working_papers", ["firm_id", "engagement_id", "status"])
    op.create_table(
        "audit_procedures",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("sa_reference", sa.String(length=100), nullable=False),
        sa.Column("procedure_name", sa.String(length=300), nullable=False),
        sa.Column("status", sa.Enum("PLANNED", "PERFORMED", "MANAGER_REVIEWED", "REWORK", "PARTNER_APPROVED", name="audit_procedure_status", native_enum=False, length=40), nullable=False),
        sa.Column("performed_by", sa.String(), nullable=False),
        sa.Column("performed_at", sa.DateTime(), nullable=True),
        sa.Column("evidence_file_paths", sa.JSON(), nullable=False),
        sa.Column("conclusion_notes", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["performed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_procedures_firm_id", "audit_procedures", ["firm_id"])
    op.create_index("ix_audit_procedures_engagement_id", "audit_procedures", ["engagement_id"])
    op.create_index("ix_audit_procedures_engagement_status", "audit_procedures", ["firm_id", "engagement_id", "status"])


def downgrade():
    op.drop_index("ix_audit_procedures_engagement_status", table_name="audit_procedures")
    op.drop_index("ix_audit_procedures_engagement_id", table_name="audit_procedures")
    op.drop_index("ix_audit_procedures_firm_id", table_name="audit_procedures")
    op.drop_table("audit_procedures")
    op.drop_index("ix_working_paper_engagement_status", table_name="audit_working_papers")
    op.drop_index("ix_audit_working_papers_engagement_id", table_name="audit_working_papers")
    op.drop_index("ix_audit_working_papers_firm_id", table_name="audit_working_papers")
    op.drop_table("audit_working_papers")
    op.drop_index("ix_mrl_engagement_status", table_name="management_rep_letters")
    op.drop_index("ix_management_rep_letters_engagement_id", table_name="management_rep_letters")
    op.drop_index("ix_management_rep_letters_firm_id", table_name="management_rep_letters")
    op.drop_table("management_rep_letters")
    op.drop_index("ix_audit_ai_records_entity", table_name="audit_ai_records")
    op.drop_index("ix_audit_ai_records_engagement", table_name="audit_ai_records")
    op.drop_index("ix_audit_ai_records_engagement_id", table_name="audit_ai_records")
    op.drop_index("ix_audit_ai_records_firm_id", table_name="audit_ai_records")
    op.drop_table("audit_ai_records")
    op.drop_column("audit_engagements", "reporting_deadline")
