"""versioned audit references and firm policy

Revision ID: a81c5d2e739b
Revises: 7f3a2c1d9b40
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a81c5d2e739b"
down_revision: Union[str, None] = "7f3a2c1d9b40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "firm_audit_policies",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("firm_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("DRAFT", "APPROVED", name="audit_policy_status",
                    native_enum=False, length=40, create_constraint=True),
            nullable=False,
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "materiality_default_basis",
            sa.Enum("TURNOVER", "PROFIT_BEFORE_TAX", "NET_WORTH",
                    name="materiality_basis", native_enum=False, length=40,
                    create_constraint=True),
            nullable=False,
        ),
        sa.Column("materiality_percentages", sa.JSON(), nullable=False),
        sa.Column("variance_threshold_pct", sa.Numeric(7, 2), nullable=False),
        sa.Column("variance_materiality_multiplier", sa.Numeric(7, 2), nullable=False),
        sa.Column("ratio_movement_threshold_pct", sa.Numeric(7, 2), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"],
                                name=op.f("fk_firm_audit_policies_approved_by_users")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name=op.f("fk_firm_audit_policies_created_by_users")),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"],
                                name=op.f("fk_firm_audit_policies_firm_id_firms")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_firm_audit_policies")),
        sa.UniqueConstraint("firm_id", "version", name="uq_audit_policy_firm_version"),
    )
    op.create_index("ix_audit_policy_firm_effective", "firm_audit_policies",
                    ["firm_id", "effective_from", "effective_to"], unique=False)
    op.create_index(op.f("ix_firm_audit_policies_firm_id"), "firm_audit_policies",
                    ["firm_id"], unique=False)

    op.create_table(
        "audit_standard_references",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("sa_number", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source_reference", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_standard_references")),
        sa.UniqueConstraint("sa_number", "version", name="uq_audit_standard_number_version"),
    )
    op.create_index("ix_audit_standard_effective", "audit_standard_references",
                    ["sa_number", "effective_from", "effective_to"], unique=False)
    op.create_index(op.f("ix_audit_standard_references_sa_number"),
                    "audit_standard_references", ["sa_number"], unique=False)

    op.create_table(
        "caro_clause_definitions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("clause_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("clause_text", sa.Text(), nullable=False),
        sa.Column("applicability_criteria", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source_reference", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_caro_clause_definitions")),
        sa.UniqueConstraint("clause_number", "version", name="uq_caro_clause_number_version"),
    )
    op.create_index("ix_caro_clause_effective", "caro_clause_definitions",
                    ["clause_number", "effective_from", "effective_to"], unique=False)
    op.create_index(op.f("ix_caro_clause_definitions_clause_number"),
                    "caro_clause_definitions", ["clause_number"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_caro_clause_definitions_clause_number"),
                  table_name="caro_clause_definitions")
    op.drop_index("ix_caro_clause_effective", table_name="caro_clause_definitions")
    op.drop_table("caro_clause_definitions")
    op.drop_index(op.f("ix_audit_standard_references_sa_number"),
                  table_name="audit_standard_references")
    op.drop_index("ix_audit_standard_effective", table_name="audit_standard_references")
    op.drop_table("audit_standard_references")
    op.drop_index(op.f("ix_firm_audit_policies_firm_id"), table_name="firm_audit_policies")
    op.drop_index("ix_audit_policy_firm_effective", table_name="firm_audit_policies")
    op.drop_table("firm_audit_policies")
