"""partner-confirmed materiality and CARO applicability planning

Revision ID: c91d4e6a20f3
Revises: b24e68f1930a
"""
from alembic import op
import sqlalchemy as sa

revision = "c91d4e6a20f3"
down_revision = "b24e68f1930a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "caro_applicability_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("rule_data", sa.JSON(), nullable=False),
        sa.Column("source_reference", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("version", name="uq_caro_applicability_rule_version"),
    )
    op.create_index("ix_caro_applicability_effective", "caro_applicability_rules", ["effective_from", "effective_to"])
    op.create_table(
        "materiality_assessments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False),
        sa.Column("engagement_id", sa.String(), sa.ForeignKey("audit_engagements.id"), nullable=False),
        sa.Column("firm_policy_id", sa.String(), sa.ForeignKey("firm_audit_policies.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("PROPOSED", "CONFIRMED", "OVERRIDDEN", "SUPERSEDED", name="audit_assessment_status", native_enum=False, length=40), nullable=False),
        sa.Column("basis", sa.Enum("TURNOVER", "PROFIT_BEFORE_TAX", "NET_WORTH", name="materiality_assessment_basis", native_enum=False, length=40), nullable=False),
        sa.Column("basis_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("percentage", sa.Numeric(9, 4), nullable=False),
        sa.Column("suggested_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("basis_rationale", sa.Text()),
        sa.Column("created_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_amount", sa.Numeric(18, 2)),
        sa.Column("confirmed_by", sa.String(), sa.ForeignKey("users.id")),
        sa.Column("confirmed_at", sa.DateTime()),
        sa.Column("override_rationale", sa.Text()),
        sa.UniqueConstraint("engagement_id", "version", name="uq_materiality_assessment_eng_version"),
    )
    op.create_index("ix_materiality_assessments_firm_id", "materiality_assessments", ["firm_id"])
    op.create_index("ix_materiality_assessments_engagement_id", "materiality_assessments", ["engagement_id"])
    op.create_index("ix_materiality_assessment_engagement", "materiality_assessments", ["firm_id", "engagement_id", "version"])
    op.create_table(
        "caro_applicability_assessments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("firm_id", sa.String(), sa.ForeignKey("firms.id"), nullable=False),
        sa.Column("engagement_id", sa.String(), sa.ForeignKey("audit_engagements.id"), nullable=False),
        sa.Column("rule_id", sa.String(), sa.ForeignKey("caro_applicability_rules.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("PROPOSED", "CONFIRMED", "OVERRIDDEN", "SUPERSEDED", name="caro_assessment_status", native_enum=False, length=40), nullable=False),
        sa.Column("determined_applicable", sa.Boolean(), nullable=False),
        sa.Column("applicable_clause_numbers", sa.JSON(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("assessed_by", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assessed_at", sa.DateTime(), nullable=False),
        sa.Column("confirmed_applicable", sa.Boolean()),
        sa.Column("confirmed_by", sa.String(), sa.ForeignKey("users.id")),
        sa.Column("confirmed_at", sa.DateTime()),
        sa.Column("confirmation_rationale", sa.Text()),
        sa.UniqueConstraint("engagement_id", "version", name="uq_caro_assessment_eng_version"),
    )
    op.create_index("ix_caro_applicability_assessments_firm_id", "caro_applicability_assessments", ["firm_id"])
    op.create_index("ix_caro_applicability_assessments_engagement_id", "caro_applicability_assessments", ["engagement_id"])
    op.create_index("ix_caro_assessment_engagement", "caro_applicability_assessments", ["firm_id", "engagement_id", "version"])
    with op.batch_alter_table("audit_engagements") as batch:
        batch.add_column(sa.Column("materiality_assessment_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("materiality_confirmed_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("materiality_confirmed_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("caro_assessment_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("caro_confirmed_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("caro_confirmed_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key("fk_audit_engagement_materiality_confirmer", "users", ["materiality_confirmed_by"], ["id"])
        batch.create_foreign_key("fk_audit_engagement_caro_confirmer", "users", ["caro_confirmed_by"], ["id"])
        batch.alter_column("materiality_amount", existing_type=sa.Numeric(14, 2), type_=sa.Numeric(18, 2), existing_nullable=True)
    op.execute(sa.text("""INSERT INTO caro_applicability_rules (id, version, effective_from, effective_to, rule_data, source_reference, created_at)
        VALUES ('caro-order-2020-v1', 1, '2021-04-01', NULL,
        '{"exempt_boolean_fields":[{"field":"is_banking_company","label":"banking company exclusion"},{"field":"is_insurance_company","label":"insurance company exclusion"},{"field":"is_section_8_company","label":"section 8 company exclusion"},{"field":"is_one_person_company","label":"one person company exclusion"},{"field":"is_small_company","label":"small company exclusion"}],"private_company_exemption":{"private_field":"is_private_company","public_group_field":"is_holding_or_subsidiary_of_public_company","criteria":[{"field":"paid_up_capital_and_reserves","operator":"lte","value":"10000000","label":"paid-up capital and reserves"},{"field":"maximum_borrowings_during_year","operator":"lte","value":"10000000","label":"borrowings during the year"},{"field":"total_revenue_including_discontinued_operations","operator":"lte","value":"100000000","label":"total revenue including discontinued operations"}]},"consolidated_only_clause_numbers":[21],"all_clause_numbers":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21]}',
        'Companies (Auditor’s Report) Order, 2020, MCA Notification S.O. 849(E), 25 February 2020; https://www.mca.gov.in/Ministry/pdf/CompaniesAuditorsReportOrder2020_25022020.pdf', CURRENT_TIMESTAMP)"""))


def downgrade():
    with op.batch_alter_table("audit_engagements") as batch:
        batch.drop_constraint("fk_audit_engagement_materiality_confirmer", type_="foreignkey")
        batch.drop_constraint("fk_audit_engagement_caro_confirmer", type_="foreignkey")
        for name in ("materiality_assessment_id", "materiality_confirmed_by", "materiality_confirmed_at", "caro_assessment_id", "caro_confirmed_by", "caro_confirmed_at"):
            batch.drop_column(name)
    op.drop_table("caro_applicability_assessments")
    op.drop_table("materiality_assessments")
    op.drop_index("ix_caro_applicability_effective", table_name="caro_applicability_rules")
    op.drop_table("caro_applicability_rules")
