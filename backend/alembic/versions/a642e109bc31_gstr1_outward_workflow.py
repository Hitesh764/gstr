"""versioned GSTR-1 outward sales preparation foundation

Revision ID: a642e109bc31
Revises: e9b4a2c7d103, c7f1d9a2e540
"""
from alembic import op
import sqlalchemy as sa
from datetime import date, datetime

revision = "a642e109bc31"
down_revision = ("e9b4a2c7d103", "c7f1d9a2e540")
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "gstr1_sales_uploads",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False, index=True),
        sa.Column("client_gstin_id", sa.String(), sa.ForeignKey("client_gstins.id"), nullable=False, index=True),
        sa.Column("period", sa.String(7), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("original_filename", sa.String(400)),
        sa.Column("file_path", sa.String(1000)),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "VALIDATED", "IMPORTED", "REJECTED", name="upload_status", native_enum=False, length=40), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("issue_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime()),
        sa.UniqueConstraint("client_gstin_id", "period", "version", name="uq_gstr1_sales_upload_version"),
    )
    op.create_table(
        "gstr1_returns",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False, index=True),
        sa.Column("client_gstin_id", sa.String(), sa.ForeignKey("client_gstins.id"), nullable=False, index=True),
        sa.Column("period", sa.String(7), nullable=False, index=True),
        sa.Column("status", sa.Enum("DRAFT", "IN_REVIEW", "APPROVED", "EXPORTED", name="gstr1_return_status", native_enum=False, length=40), nullable=False),
        sa.Column("rule_version", sa.String(40), nullable=False),
        sa.Column("source_upload_id", sa.String(), sa.ForeignKey("gstr1_sales_uploads.id")),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reviewed_by", sa.String(), sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", sa.DateTime()),
        sa.Column("approved_by", sa.String(), sa.ForeignKey("users.id")),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime()),
        sa.UniqueConstraint("client_gstin_id", "period", name="uq_gstr1_return_gstin_period"),
    )
    op.create_table(
        "gstr1_sales_invoices",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False, index=True),
        sa.Column("upload_id", sa.String(), sa.ForeignKey("gstr1_sales_uploads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False), sa.Column("customer_name", sa.String(300)),
        sa.Column("customer_gstin", sa.String(15), index=True), sa.Column("invoice_number", sa.String(100), nullable=False),
        sa.Column("normalized_invoice_number", sa.String(100), nullable=False, index=True),
        sa.Column("document_series", sa.String(100), index=True),
        sa.Column("invoice_date", sa.Date(), nullable=False), sa.Column("invoice_value", sa.Numeric(16, 2), nullable=False),
        sa.Column("taxable_value", sa.Numeric(16, 2), nullable=False), sa.Column("cgst", sa.Numeric(16, 2), nullable=False),
        sa.Column("sgst", sa.Numeric(16, 2), nullable=False), sa.Column("igst", sa.Numeric(16, 2), nullable=False),
        sa.Column("cess", sa.Numeric(16, 2), nullable=False), sa.Column("gst_rate", sa.Numeric(7, 3)),
        sa.Column("place_of_supply", sa.String(2)), sa.Column("hsn_sac", sa.String(16)),
        sa.Column("supply_type", sa.String(30), nullable=False), sa.Column("document_type", sa.String(30), nullable=False),
        sa.Column("classification", sa.String(30), nullable=False), sa.Column("classification_reason", sa.String(250), nullable=False),
        sa.Column("status", sa.Enum("SYSTEM_FLAGGED", "NEEDS_REVIEW", "REVIEWED", name="gstr1_invoice_status", native_enum=False, length=40), nullable=False),
        sa.Column("reviewed_by", sa.String(), sa.ForeignKey("users.id")), sa.Column("reviewed_at", sa.DateTime()),
    )
    op.create_index("ix_gstr1_invoice_upload", "gstr1_sales_invoices", ["firm_id", "upload_id"])
    op.create_table(
        "gstr1_validation_issues",
        sa.Column("id", sa.String(), primary_key=True), sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False, index=True),
        sa.Column("upload_id", sa.String(), sa.ForeignKey("gstr1_sales_uploads.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("invoice_id", sa.String(), sa.ForeignKey("gstr1_sales_invoices.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("row_number", sa.Integer()),
        sa.Column("code", sa.String(60), nullable=False), sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reviewed_by", sa.String(), sa.ForeignKey("users.id")), sa.Column("reviewed_at", sa.DateTime()),
    )
    op.create_table(
        "gstr1_rule_definitions",
        sa.Column("id", sa.String(), primary_key=True), sa.Column("rule_key", sa.String(80), nullable=False, index=True),
        sa.Column("version", sa.String(40), nullable=False), sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.String(500), nullable=False), sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date()), sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("rule_key", "effective_from", name="uq_gstr1_rule_version"),
    )
    rules = sa.table("gstr1_rule_definitions",
        sa.column("id", sa.String()), sa.column("rule_key", sa.String()),
        sa.column("version", sa.String()), sa.column("definition", sa.JSON()),
        sa.column("source_url", sa.String()), sa.column("effective_from", sa.Date()),
        sa.column("effective_to", sa.Date()), sa.column("verified_at", sa.DateTime()))
    op.bulk_insert(rules, [
        {"id": "gstr1-b2cl-threshold-2024-08", "rule_key": "B2CL_THRESHOLD",
         "version": "GST_PORTAL_B2CL_PRE_AUG_2024", "definition": {"threshold_inr": "250000", "basis": "invoice_value", "scope": "inter_state_unregistered"},
         "source_url": "https://tutorial.gst.gov.in/userguide/returns/Creation_of_Outward_Supplies_Return_in_GSTR-1.htm",
         "effective_from": date(2017, 7, 1), "effective_to": date(2024, 7, 31),
         "verified_at": datetime.utcnow()},
        {"id": "gstr1-b2cl-threshold-2024-08-current", "rule_key": "B2CL_THRESHOLD",
         "version": "GST_PORTAL_B2CL_AUG_2024", "definition": {"threshold_inr": "100000", "basis": "invoice_value", "scope": "inter_state_unregistered"},
         "source_url": "https://tutorial.gst.gov.in/userguide/returns/Creation_of_Outward_Supplies_Return_in_GSTR-1.htm",
         "effective_from": date(2024, 8, 1), "effective_to": None,
         "verified_at": datetime.utcnow()},
    ])
    op.create_table(
        "gstr1_documents_issued",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False, index=True),
        sa.Column("return_id", sa.String(), sa.ForeignKey("gstr1_returns.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("document_type", sa.String(40), nullable=False),
        sa.Column("series_from", sa.String(100), nullable=False),
        sa.Column("series_to", sa.String(100), nullable=False),
        sa.Column("issued_count", sa.Integer(), nullable=False),
        sa.Column("cancelled_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("prepared_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("prepared_at", sa.DateTime(), nullable=False),
        sa.Column("reviewed_by", sa.String(), sa.ForeignKey("users.id")),
        sa.Column("reviewed_at", sa.DateTime()),
        sa.UniqueConstraint("return_id", "document_type", "series_from", "version", name="uq_gstr1_document_series_version"),
    )


def downgrade():
    op.drop_table("gstr1_documents_issued")
    op.drop_table("gstr1_rule_definitions")
    op.drop_table("gstr1_validation_issues")
    op.drop_index("ix_gstr1_invoice_upload", table_name="gstr1_sales_invoices")
    op.drop_table("gstr1_sales_invoices")
    op.drop_table("gstr1_returns")
    op.drop_table("gstr1_sales_uploads")
