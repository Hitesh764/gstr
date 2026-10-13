"""source-attributed statutory filing data for audit engagements

Revision ID: f6a4c2e1b790
Revises: d3e7a19c54b2
"""
from alembic import op
import sqlalchemy as sa

revision = "f6a4c2e1b790"
down_revision = "d3e7a19c54b2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "statutory_filing_records",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("tax_type", sa.Enum("GST", "TDS", "PF", "ESI", name="statutory_tax_type", native_enum=False, length=40), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("filing_status", sa.Enum("FILED", "PENDING", "ARREARS", "NOT_APPLICABLE", name="statutory_filing_status", native_enum=False, length=40), nullable=False),
        sa.Column("filing_source", sa.Enum("PORTAL_RECORD", "CLIENT_RECORD", "MANAGEMENT_REPRESENTATION", "OTHER", name="statutory_filing_source", native_enum=False, length=40), nullable=False),
        sa.Column("filed_at", sa.Date(), nullable=True),
        sa.Column("statutory_due_date", sa.Date(), nullable=True),
        sa.Column("amount_due", sa.Numeric(18, 2), nullable=True),
        sa.Column("amount_paid", sa.Numeric(18, 2), nullable=True),
        sa.Column("source_reference", sa.String(length=500), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "tax_type", "period", "version", name="uq_statutory_filing_eng_tax_period_version"),
    )
    op.create_index("ix_statutory_filing_records_firm_id", "statutory_filing_records", ["firm_id"])
    op.create_index("ix_statutory_filing_records_engagement_id", "statutory_filing_records", ["engagement_id"])
    op.create_index("ix_statutory_filing_records_period", "statutory_filing_records", ["period"])
    op.create_index("ix_statutory_filing_eng_period", "statutory_filing_records", ["firm_id", "engagement_id", "period"])


def downgrade():
    op.drop_index("ix_statutory_filing_eng_period", table_name="statutory_filing_records")
    op.drop_index("ix_statutory_filing_records_period", table_name="statutory_filing_records")
    op.drop_index("ix_statutory_filing_records_engagement_id", table_name="statutory_filing_records")
    op.drop_index("ix_statutory_filing_records_firm_id", table_name="statutory_filing_records")
    op.drop_table("statutory_filing_records")
