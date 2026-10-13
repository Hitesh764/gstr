"""firm-maintained effective-dated GST rate reference lists

Revision ID: 72a4f1c9b861
Revises: c0e8f31a52d7
"""
from alembic import op
import sqlalchemy as sa

revision = "72a4f1c9b861"
down_revision = "c0e8f31a52d7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gstr1_sales_uploads", sa.Column("rate_reference_snapshot", sa.JSON(), nullable=True))
    op.create_table(
        "gstr1_tenant_rate_references",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(length=60), nullable=False),
        sa.Column("rates_by_hsn", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("uploaded_by", sa.String(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("firm_id", "version", name="uq_gstr1_tenant_rate_reference_version"),
    )
    op.create_index("ix_gstr1_tenant_rate_references_firm_id", "gstr1_tenant_rate_references", ["firm_id"])


def downgrade():
    op.drop_index("ix_gstr1_tenant_rate_references_firm_id", table_name="gstr1_tenant_rate_references")
    op.drop_table("gstr1_tenant_rate_references")
    op.drop_column("gstr1_sales_uploads", "rate_reference_snapshot")
