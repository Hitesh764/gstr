"""audit engagement foundation

Revision ID: 7f3a2c1d9b40
Revises: 182eb8deeead, 9c5e2f7a1b30
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7f3a2c1d9b40"
down_revision: Union[str, Sequence[str], None] = ("182eb8deeead", "9c5e2f7a1b30")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_engagements",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("financial_year", sa.String(length=7), nullable=False),
        sa.Column(
            "engagement_type",
            sa.Enum("STATUTORY", "TAX", "INTERNAL", name="audit_engagement_type",
                    native_enum=False, length=40, create_constraint=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("PLANNING", "FIELDWORK", "COMPLETION", "REPORTING", "CLOSED",
                    name="audit_engagement_status", native_enum=False, length=40,
                    create_constraint=True),
            nullable=False,
        ),
        sa.Column("engagement_partner_id", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("materiality_amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("materiality_basis", sa.Text(), nullable=True),
        sa.Column("caro_applicable", sa.Boolean(), nullable=True),
        sa.Column("caro_applicability_reasoning", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"],
                                name=op.f("fk_audit_engagements_client_id_clients")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name=op.f("fk_audit_engagements_created_by_users")),
        sa.ForeignKeyConstraint(["engagement_partner_id"], ["users.id"],
                                name=op.f("fk_audit_engagements_engagement_partner_id_users")),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"],
                                name=op.f("fk_audit_engagements_firm_id_firms")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_engagements")),
        sa.UniqueConstraint("client_id", "financial_year", "engagement_type",
                            name="uq_audit_engagement_client_year_type"),
    )
    op.create_index("ix_audit_engagement_firm_status", "audit_engagements",
                    ["firm_id", "status"], unique=False)
    op.create_index("ix_audit_engagement_client_year", "audit_engagements",
                    ["client_id", "financial_year"], unique=False)
    op.create_index(op.f("ix_audit_engagements_firm_id"), "audit_engagements",
                    ["firm_id"], unique=False)
    op.create_index(op.f("ix_audit_engagements_client_id"), "audit_engagements",
                    ["client_id"], unique=False)
    op.create_index(op.f("ix_audit_engagements_financial_year"), "audit_engagements",
                    ["financial_year"], unique=False)
    op.create_index(op.f("ix_audit_engagements_engagement_partner_id"),
                    "audit_engagements", ["engagement_partner_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_engagements_engagement_partner_id"),
                  table_name="audit_engagements")
    op.drop_index(op.f("ix_audit_engagements_financial_year"),
                  table_name="audit_engagements")
    op.drop_index(op.f("ix_audit_engagements_client_id"), table_name="audit_engagements")
    op.drop_index(op.f("ix_audit_engagements_firm_id"), table_name="audit_engagements")
    op.drop_index("ix_audit_engagement_client_year", table_name="audit_engagements")
    op.drop_index("ix_audit_engagement_firm_status", table_name="audit_engagements")
    op.drop_table("audit_engagements")
