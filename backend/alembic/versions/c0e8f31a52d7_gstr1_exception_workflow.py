"""add GSTR-1 source metadata and exception decisions

Revision ID: c0e8f31a52d7
Revises: b81c7f2d1a40
"""
from alembic import op
import sqlalchemy as sa

revision = "c0e8f31a52d7"
down_revision = "b81c7f2d1a40"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gstr1_returns", sa.Column("reconciliation_result", sa.JSON(), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("description", sa.String(500), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("original_invoice_number", sa.String(100), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("invoice_status", sa.String(30), nullable=False, server_default="ACTIVE"))
    op.add_column("gstr1_validation_issues", sa.Column("review_decision", sa.String(30), nullable=True))
    op.add_column("gstr1_validation_issues", sa.Column("review_note", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("gstr1_validation_issues", "review_note")
    op.drop_column("gstr1_validation_issues", "review_decision")
    op.drop_column("gstr1_sales_invoices", "invoice_status")
    op.drop_column("gstr1_sales_invoices", "original_invoice_number")
    op.drop_column("gstr1_sales_invoices", "description")
    op.drop_column("gstr1_returns", "reconciliation_result")
