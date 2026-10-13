"""encrypted client bank accounts and approved confirmation tracking

Revision ID: b7c1e20f94a6
Revises: e2f9a641c5b8
"""
from alembic import op
import sqlalchemy as sa

revision = "b7c1e20f94a6"
down_revision = "e2f9a641c5b8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "client_bank_accounts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("bank_name", sa.String(length=200), nullable=False),
        sa.Column("branch_name", sa.String(length=200), nullable=True),
        sa.Column("account_name", sa.String(length=200), nullable=True),
        sa.Column("account_number_encrypted", sa.Text(), nullable=False),
        sa.Column("account_number_last4", sa.String(length=4), nullable=False),
        sa.Column("account_number_blind_index", sa.String(length=64), nullable=False),
        sa.Column("ifsc_code", sa.String(length=11), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "account_number_blind_index", name="uq_client_bank_account_fingerprint"),
    )
    op.create_index("ix_client_bank_accounts_firm_id", "client_bank_accounts", ["firm_id"])
    op.create_index("ix_client_bank_accounts_client_id", "client_bank_accounts", ["client_id"])
    op.create_index("ix_client_bank_accounts_account_number_blind_index", "client_bank_accounts", ["account_number_blind_index"])
    op.create_index("ix_client_bank_accounts_client_active", "client_bank_accounts", ["firm_id", "client_id", "is_active"])
    op.create_table(
        "bank_confirmations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("engagement_id", sa.String(), nullable=False),
        sa.Column("bank_account_id", sa.String(), nullable=False),
        sa.Column("account_number_last4", sa.String(length=4), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("letter_status", sa.Enum("DRAFTED", "SENT", "PENDING", "RECEIVED", "RECONCILED", name="bank_confirmation_status", native_enum=False, length=40), nullable=False),
        sa.Column("draft_content", sa.Text(), nullable=False),
        sa.Column("drafted_by", sa.String(), nullable=False),
        sa.Column("drafted_at", sa.DateTime(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("sent_by", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("books_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("confirmed_balance", sa.Numeric(18, 2), nullable=True),
        sa.Column("discrepancy_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("discrepancy_notes", sa.Text(), nullable=True),
        sa.Column("reconciled_by", sa.String(), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["engagement_id"], ["audit_engagements.id"]),
        sa.ForeignKeyConstraint(["bank_account_id"], ["client_bank_accounts.id"]),
        sa.ForeignKeyConstraint(["drafted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["sent_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reconciled_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("engagement_id", "bank_account_id", "as_of_date", "version", name="uq_bank_confirmation_eng_account_date_version"),
    )
    op.create_index("ix_bank_confirmations_firm_id", "bank_confirmations", ["firm_id"])
    op.create_index("ix_bank_confirmations_engagement_id", "bank_confirmations", ["engagement_id"])
    op.create_index("ix_bank_confirmations_bank_account_id", "bank_confirmations", ["bank_account_id"])
    op.create_index("ix_bank_confirmation_engagement_status", "bank_confirmations", ["firm_id", "engagement_id", "letter_status"])


def downgrade():
    op.drop_index("ix_bank_confirmation_engagement_status", table_name="bank_confirmations")
    op.drop_index("ix_bank_confirmations_bank_account_id", table_name="bank_confirmations")
    op.drop_index("ix_bank_confirmations_engagement_id", table_name="bank_confirmations")
    op.drop_index("ix_bank_confirmations_firm_id", table_name="bank_confirmations")
    op.drop_table("bank_confirmations")
    op.drop_index("ix_client_bank_accounts_client_active", table_name="client_bank_accounts")
    op.drop_index("ix_client_bank_accounts_account_number_blind_index", table_name="client_bank_accounts")
    op.drop_index("ix_client_bank_accounts_client_id", table_name="client_bank_accounts")
    op.drop_index("ix_client_bank_accounts_firm_id", table_name="client_bank_accounts")
    op.drop_table("client_bank_accounts")
