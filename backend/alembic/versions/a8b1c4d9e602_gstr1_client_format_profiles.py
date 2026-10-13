"""remember resolved GSTR-1 sales-register layouts per client and format

Revision ID: a8b1c4d9e602
Revises: e2f9a641c5b8, 72a4f1c9b861
"""
from alembic import op
import sqlalchemy as sa

revision = "a8b1c4d9e602"
down_revision = ("e2f9a641c5b8", "72a4f1c9b861")
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gstr1_sales_uploads", sa.Column("normalization_snapshot", sa.JSON(), nullable=True))
    op.create_table(
        "gstr1_client_format_profiles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("client_gstin_id", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("source_name", sa.String(length=100), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_columns", sa.JSON(), nullable=False),
        sa.Column("column_rules", sa.JSON(), nullable=False),
        sa.Column("confirmed_columns", sa.JSON(), nullable=False),
        sa.Column("alias_version", sa.String(length=60), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("import_count", sa.Integer(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["client_gstin_id"], ["client_gstins.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_gstin_id", "source_type", "fingerprint", name="uq_gstr1_client_format_fingerprint"),
    )
    op.create_index("ix_gstr1_client_format_profiles_firm_id", "gstr1_client_format_profiles", ["firm_id"])
    op.create_index("ix_gstr1_client_format_profiles_client_gstin_id", "gstr1_client_format_profiles", ["client_gstin_id"])
    op.create_index("ix_gstr1_client_format_profiles_fingerprint", "gstr1_client_format_profiles", ["fingerprint"])


def downgrade():
    op.drop_index("ix_gstr1_client_format_profiles_fingerprint", table_name="gstr1_client_format_profiles")
    op.drop_index("ix_gstr1_client_format_profiles_client_gstin_id", table_name="gstr1_client_format_profiles")
    op.drop_index("ix_gstr1_client_format_profiles_firm_id", table_name="gstr1_client_format_profiles")
    op.drop_table("gstr1_client_format_profiles")
    op.drop_column("gstr1_sales_uploads", "normalization_snapshot")
