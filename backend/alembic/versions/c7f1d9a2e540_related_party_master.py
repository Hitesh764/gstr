"""client related-party master and stable source references for review flags

Revision ID: c7f1d9a2e540
Revises: a4d8f19c2e60
"""
from alembic import op
import sqlalchemy as sa

revision = "c7f1d9a2e540"
down_revision = "a4d8f19c2e60"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("risk_flags", sa.Column("source_reference_key", sa.String(length=300), nullable=True))
    op.create_index("uq_risk_flag_source_reference_key", "risk_flags", ["source_reference_key"], unique=True)
    op.create_table(
        "client_related_parties",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("normalized_name", sa.String(length=300), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("pan", sa.String(length=10), nullable=True),
        sa.Column("relationship_nature", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("updated_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "normalized_name", name="uq_client_related_party_name"),
    )
    op.create_index("ix_client_related_parties_firm_id", "client_related_parties", ["firm_id"])
    op.create_index("ix_client_related_parties_client_id", "client_related_parties", ["client_id"])
    op.create_index("ix_related_parties_client_active", "client_related_parties", ["firm_id", "client_id", "is_active"])


def downgrade():
    op.drop_index("ix_related_parties_client_active", table_name="client_related_parties")
    op.drop_index("ix_client_related_parties_client_id", table_name="client_related_parties")
    op.drop_index("ix_client_related_parties_firm_id", table_name="client_related_parties")
    op.drop_table("client_related_parties")
    op.drop_index("uq_risk_flag_source_reference_key", table_name="risk_flags")
    op.drop_column("risk_flags", "source_reference_key")
