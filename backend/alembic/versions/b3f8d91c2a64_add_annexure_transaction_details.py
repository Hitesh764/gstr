"""store optional sales details needed by transaction annexures

Revision ID: b3f8d91c2a64
Revises: a8b1c4d9e602
"""
from alembic import op
import sqlalchemy as sa

revision = "b3f8d91c2a64"
down_revision = "a8b1c4d9e602"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gstr1_sales_invoices", sa.Column("original_invoice_date", sa.Date(), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("note_reason", sa.String(length=300), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("quantity", sa.Numeric(16, 3), nullable=False, server_default="0"))
    op.add_column("gstr1_sales_invoices", sa.Column("uqc", sa.String(length=30), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("export_type", sa.String(length=80), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("shipping_bill_number", sa.String(length=100), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("shipping_bill_date", sa.Date(), nullable=True))
    op.add_column("gstr1_sales_invoices", sa.Column("port_code", sa.String(length=30), nullable=True))


def downgrade():
    op.drop_column("gstr1_sales_invoices", "port_code")
    op.drop_column("gstr1_sales_invoices", "shipping_bill_date")
    op.drop_column("gstr1_sales_invoices", "shipping_bill_number")
    op.drop_column("gstr1_sales_invoices", "export_type")
    op.drop_column("gstr1_sales_invoices", "uqc")
    op.drop_column("gstr1_sales_invoices", "quantity")
    op.drop_column("gstr1_sales_invoices", "note_reason")
    op.drop_column("gstr1_sales_invoices", "original_invoice_date")
