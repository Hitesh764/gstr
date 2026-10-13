"""add retention metadata to GSTR-1 soft-deletable tables

Revision ID: b81c7f2d1a40
Revises: a642e109bc31
"""
from alembic import op
import sqlalchemy as sa

revision = "b81c7f2d1a40"
down_revision = "a642e109bc31"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gstr1_returns", sa.Column("retention_until", sa.DateTime(), nullable=True))
    op.add_column("gstr1_sales_uploads", sa.Column("retention_until", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("gstr1_sales_uploads", "retention_until")
    op.drop_column("gstr1_returns", "retention_until")
