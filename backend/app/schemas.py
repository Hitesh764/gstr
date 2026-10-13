"""Pydantic schemas for API request/response bodies."""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import (
    UserRole, ClientStatus, EntityType, EngagementType,
    ExceptionStatus, MatchStatus, SubscriptionTier, SubscriptionStatus,
    AuditEngagementType, AuditEngagementStatus, AuditPolicyStatus, MaterialityBasis,
    TrialBalancePeriodType, AuditAssessmentStatus, RiskFlagType, RiskFlagStatus,
    StatutoryTaxType, StatutoryFilingStatus, StatutoryFilingSource,
    AgeingType,
    BankConfirmationStatus,
    RatioAnalysisStatus,
    CAROResponseStatus,
    ManagementRepLetterStatus,
    WorkingPaperStatus,
    AuditProcedureStatus,
)
from app.validation import normalize_gstin, validate_gstin, validate_pan


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class FirmSignupIn(BaseModel):
    """Creates the firm (tenant) and its first OWNER user in one step."""
    firm_name: str = Field(..., min_length=2, max_length=200)
    full_name: str = Field(..., min_length=2, max_length=200)
    email: EmailStr
    phone: Optional[str] = Field(None, max_length=20)
    password: str = Field(..., min_length=10, max_length=256)
    firm_gstin: Optional[str] = Field(None, max_length=15)
    firm_pan: Optional[str] = Field(None, max_length=10)

    @field_validator("firm_gstin")
    @classmethod
    def _check_gstin(cls, v):
        if not v:
            return None
        v = normalize_gstin(v)
        ok, msg = validate_gstin(v)
        if not ok:
            raise ValueError(msg)
        return v

    @field_validator("firm_pan")
    @classmethod
    def _check_pan(cls, v):
        if not v:
            return None
        v = v.strip().upper()
        ok, msg = validate_pan(v)
        if not ok:
            raise ValueError(msg)
        return v


class LoginIn(BaseModel):
    email: EmailStr
    password: str
    totp_code: Optional[str] = Field(None, max_length=10)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    firm_id: str
    name: str
    email: str
    phone: Optional[str] = None
    role: UserRole
    is_active: bool
    email_verified: bool
    phone_verified: bool
    totp_enabled: bool
    scoped_client_id: Optional[str] = None
    last_login_at: Optional[datetime] = None
    created_at: datetime


class FirmOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    contact_email: str
    contact_phone: Optional[str] = None
    gstin: Optional[str] = None
    pan: Optional[str] = None
    subscription_tier: SubscriptionTier
    subscription_status: SubscriptionStatus
    require_2fa_for_partners: bool
    created_at: datetime


class TokenOut(BaseModel):
    """The refresh token is set as an httpOnly cookie, never returned here."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int          # seconds
    user: UserOut
    firm: FirmOut


class InviteUserIn(BaseModel):
    email: EmailStr
    role: UserRole = UserRole.ASSOCIATE
    scoped_client_id: Optional[str] = None

    @field_validator("role")
    @classmethod
    def _no_owner_invites(cls, v):
        if v == UserRole.OWNER:
            raise ValueError(
                "A firm has exactly one owner. Invite a Partner instead, or "
                "transfer ownership explicitly."
            )
        return v


class InvitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    role: UserRole
    expires_at: datetime
    accepted_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: datetime


class AcceptInviteIn(BaseModel):
    token: str
    full_name: str = Field(..., min_length=2, max_length=200)
    password: str = Field(..., min_length=10, max_length=256)
    phone: Optional[str] = Field(None, max_length=20)


class PasswordResetRequestIn(BaseModel):
    email: EmailStr


class PasswordResetConfirmIn(BaseModel):
    token: str
    new_password: str = Field(..., min_length=10, max_length=256)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=10, max_length=256)


class TOTPSetupOut(BaseModel):
    secret: str
    provisioning_uri: str


class TOTPVerifyIn(BaseModel):
    code: str = Field(..., min_length=6, max_length=10)


class MessageOut(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Clients & GSTINs
# ---------------------------------------------------------------------------

class ClientGSTINIn(BaseModel):
    gstin: str
    is_primary: bool = False

    @field_validator("gstin")
    @classmethod
    def _check(cls, v):
        v = normalize_gstin(v)
        ok, msg = validate_gstin(v)
        if not ok:
            raise ValueError(msg)
        return v


class ClientGSTINOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_id: str
    gstin: str
    state_code: Optional[str] = None
    state_name: Optional[str] = None
    legal_name: Optional[str] = None
    trade_name: Optional[str] = None
    is_primary: bool
    verified_at: Optional[datetime] = None
    consent_granted_at: Optional[datetime] = None
    consent_expires_at: Optional[datetime] = None
    has_active_consent: bool = False
    # NONE | ACTIVE | EXPIRING | EXPIRED. Derived from the consent's own
    # timestamps, so the client list can show live-access status without a
    # request per GSTIN.
    gsp_state: str = "NONE"
    gsp_provider: Optional[str] = None


class ClientCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=200)
    pan: Optional[str] = None
    entity_type: EntityType = EntityType.OTHER
    engagement_type: EngagementType = EngagementType.COMPLIANCE_ONLY
    status: ClientStatus = ClientStatus.ONBOARDING
    contact_name: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = None
    assigned_associate_id: Optional[str] = None
    reconciliation_frequency: str = "MONTHLY"
    gstins: list[ClientGSTINIn] = Field(default_factory=list)

    @field_validator("pan")
    @classmethod
    def _check_pan(cls, v):
        if not v:
            return None
        v = v.strip().upper()
        ok, msg = validate_pan(v)
        if not ok:
            raise ValueError(msg)
        return v


class ClientUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=200)
    pan: Optional[str] = None
    entity_type: Optional[EntityType] = None
    engagement_type: Optional[EngagementType] = None
    status: Optional[ClientStatus] = None
    contact_name: Optional[str] = None
    contact_email: Optional[EmailStr] = None
    contact_phone: Optional[str] = None
    assigned_associate_id: Optional[str] = None
    reconciliation_frequency: Optional[str] = None


class ClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    firm_id: str
    name: str
    pan: Optional[str] = None
    entity_type: EntityType
    engagement_type: EngagementType
    status: ClientStatus
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    assigned_associate_id: Optional[str] = None
    reconciliation_frequency: str
    created_at: datetime
    gstins: list[ClientGSTINOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------

class PurchaseEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vendor_name: str
    vendor_gstin: str
    invoice_number: str
    invoice_date: date
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total_value: Decimal


class GSTR2BEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    vendor_name: Optional[str] = None
    vendor_gstin: str
    invoice_number: str
    invoice_date: date
    taxable_value: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total_value: Decimal
    filing_status: str


# ---------------------------------------------------------------------------
# Uploads / fetches
# ---------------------------------------------------------------------------

class RowError(BaseModel):
    row: Optional[int] = None
    field: Optional[str] = None
    message: str
    severity: str = "error"    # error | warning


class UploadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_gstin_id: str
    period: str
    version: int
    is_current: bool
    source: str
    original_filename: Optional[str] = None
    status: str
    row_count: int
    error_count: int
    uploaded_at: datetime
    uploaded_by: Optional[str] = None


class UploadResultOut(BaseModel):
    upload: Optional[UploadOut] = None
    inserted: int
    errors: list[RowError] = Field(default_factory=list)
    warnings: list[RowError] = Field(default_factory=list)
    accepted: bool


class FetchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_gstin_id: str
    period: str
    version: int
    is_current: bool
    source: str
    gsp_provider: Optional[str] = None
    generated_on: Optional[date] = None
    fetched_at: datetime


class FetchResultOut(BaseModel):
    fetch: Optional[FetchOut] = None
    inserted: int
    errors: list[RowError] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class ReconciliationRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_gstin_id: str
    period: str
    version: int
    is_current: bool
    status: str
    run_at: datetime
    completed_at: Optional[datetime] = None
    run_by: Optional[str] = None


class ExceptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: ExceptionStatus
    assigned_to: Optional[str] = None
    resolution_notes: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rework_reason: Optional[str] = None
    itc_lapse_deadline: Optional[date] = None


class ReconciliationResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    run_id: str
    match_status: MatchStatus
    confidence_score: int
    plain_language_note: Optional[str] = None
    match_key: Optional[str] = None
    purchase_entry: Optional[PurchaseEntryOut] = None
    gstr2b_entry: Optional[GSTR2BEntryOut] = None
    exception: Optional[ExceptionOut] = None


class DashboardSummary(BaseModel):
    period: str
    client_gstin_id: str
    run_id: Optional[str] = None
    total_itc_in_books: float
    total_itc_eligible_2b: float
    total_itc_blocked_2b: float = 0.0
    total_itc_at_risk: float
    pct_auto_matched: float
    counts_by_status: dict[str, int]
    total_entries: int
    exceptions_open: int = 0
    exceptions_pending_approval: int = 0


class ResolveExceptionIn(BaseModel):
    resolution_notes: str = Field(..., min_length=1)


class ApproveExceptionIn(BaseModel):
    approved: bool
    rework_reason: Optional[str] = None


class ExceptionCommentIn(BaseModel):
    comment: str = Field(..., min_length=1, max_length=4000)


class ExceptionCommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    exception_id: str
    user_id: str
    comment: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_id: Optional[str] = None
    action: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    meta: Optional[Any] = None
    ip_address: Optional[str] = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Audit engagements
# ---------------------------------------------------------------------------

class AuditEngagementCreate(BaseModel):
    client_id: str
    financial_year: str = Field(..., pattern=r"^20\d{2}-\d{2}$")
    engagement_type: AuditEngagementType
    engagement_partner_id: str
    reporting_deadline: Optional[date] = None

    @field_validator("financial_year")
    @classmethod
    def _validate_financial_year(cls, value: str) -> str:
        start, end = (int(part) for part in value.split("-"))
        if end != (start + 1) % 100:
            raise ValueError("Financial year must be consecutive, for example 2025-26.")
        return value


class AuditEngagementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_id: str
    financial_year: str
    engagement_type: AuditEngagementType
    status: AuditEngagementStatus
    engagement_partner_id: str
    reporting_deadline: Optional[date] = None
    created_by: str
    materiality_amount: Optional[Decimal] = None
    materiality_basis: Optional[str] = None
    caro_applicable: Optional[bool] = None
    caro_applicability_reasoning: Optional[str] = None
    materiality_assessment_id: Optional[str] = None
    materiality_confirmed_by: Optional[str] = None
    materiality_confirmed_at: Optional[datetime] = None
    caro_assessment_id: Optional[str] = None
    caro_confirmed_by: Optional[str] = None
    caro_confirmed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class MaterialitySuggestIn(BaseModel):
    turnover: Decimal = Field(..., ge=0)
    profit_before_tax: Decimal = Field(..., ge=0)
    net_worth: Decimal = Field(..., ge=0)
    basis: Optional[MaterialityBasis] = None
    basis_rationale: Optional[str] = Field(None, max_length=2000)


class MaterialityConfirmIn(BaseModel):
    confirmed_amount: Optional[Decimal] = Field(None, gt=0)
    override_rationale: Optional[str] = Field(None, min_length=10, max_length=2000)


class MaterialityAssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    firm_policy_id: str
    version: int
    status: AuditAssessmentStatus
    basis: MaterialityBasis
    basis_amount: Decimal
    percentage: Decimal
    suggested_amount: Decimal
    inputs: dict
    basis_rationale: Optional[str]
    created_by: str
    created_at: datetime
    confirmed_amount: Optional[Decimal]
    confirmed_by: Optional[str]
    confirmed_at: Optional[datetime]
    override_rationale: Optional[str]


class CAROApplicabilityAssessIn(BaseModel):
    is_banking_company: bool = False
    is_insurance_company: bool = False
    is_section_8_company: bool = False
    is_one_person_company: bool = False
    is_small_company: bool = False
    is_private_company: bool = False
    is_holding_or_subsidiary_of_public_company: bool = False
    is_consolidated_financial_statements: bool = False
    paid_up_capital_and_reserves: Decimal = Field(..., ge=0)
    maximum_borrowings_during_year: Decimal = Field(..., ge=0)
    total_revenue_including_discontinued_operations: Decimal = Field(..., ge=0)


class CAROApplicabilityConfirmIn(BaseModel):
    applicable: bool
    rationale: Optional[str] = Field(None, min_length=10, max_length=2000)


class CAROApplicabilityAssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    rule_id: str
    version: int
    status: AuditAssessmentStatus
    determined_applicable: bool
    applicable_clause_numbers: list[int]
    reasoning: str
    inputs: dict
    assessed_by: str
    assessed_at: datetime
    confirmed_applicable: Optional[bool]
    confirmed_by: Optional[str]
    confirmed_at: Optional[datetime]
    confirmation_rationale: Optional[str]


class FirmAuditPolicyCreate(BaseModel):
    effective_from: date
    effective_to: Optional[date] = None
    materiality_percentages: dict[MaterialityBasis, Decimal]
    materiality_default_basis: MaterialityBasis
    variance_threshold_pct: Decimal = Field(..., ge=0, le=100)
    variance_materiality_multiplier: Decimal = Field(..., ge=0, le=100)
    ratio_movement_threshold_pct: Decimal = Field(..., ge=0, le=1000)

    @field_validator("effective_to")
    @classmethod
    def _effective_range_is_valid(cls, value: Optional[date], info):
        starts = info.data.get("effective_from")
        if value is not None and starts is not None and value < starts:
            raise ValueError("effective_to must be on or after effective_from.")
        return value

    @field_validator("materiality_percentages")
    @classmethod
    def _valid_materiality_percentages(cls, value):
        if not value:
            raise ValueError("Configure a materiality percentage for at least one basis.")
        if any(percent <= 0 or percent > 100 for percent in value.values()):
            raise ValueError("Materiality percentages must be greater than 0 and at most 100.")
        return value

    @field_validator("materiality_default_basis")
    @classmethod
    def _default_basis_must_be_configured(cls, value, info):
        percentages = info.data.get("materiality_percentages") or {}
        if value not in percentages:
            raise ValueError("The default materiality basis must have a configured percentage.")
        return value


class FirmAuditPolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    version: int
    status: AuditPolicyStatus
    effective_from: date
    effective_to: Optional[date] = None
    materiality_default_basis: MaterialityBasis
    materiality_percentages: dict[str, Decimal]
    variance_threshold_pct: Decimal
    variance_materiality_multiplier: Decimal
    ratio_movement_threshold_pct: Decimal
    created_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class AuditStandardReferenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    sa_number: str
    title: str
    summary: str
    version: int
    effective_from: date
    effective_to: Optional[date] = None
    source_reference: str


class CAROClauseDefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    clause_number: int
    title: str
    clause_text: str
    applicability_criteria: dict[str, Any]
    version: int
    effective_from: date
    effective_to: Optional[date] = None
    source_reference: str


class CAROClauseResponseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    clause_definition_id: str
    clause_number: int
    version: int
    applicability: str
    applicability_reasoning: str
    draft_narrative: str
    final_narrative: Optional[str]
    source_snapshot: dict[str, Any]
    status: CAROResponseStatus
    prepared_by: str
    prepared_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]
    finalized_by: Optional[str]
    finalized_at: Optional[datetime]


class CAROResponseEditIn(BaseModel):
    final_narrative: str = Field(..., min_length=1, max_length=20000)


class CAROResponseReviewIn(BaseModel):
    decision: str = Field(..., pattern=r"^(approve|rework)$")
    notes: Optional[str] = Field(None, max_length=4000)


class CAROResponseFinalizeIn(BaseModel):
    final_narrative: str = Field(..., min_length=1, max_length=20000)


class ManagementRepLetterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    version: int
    template_version: str
    draft_content: str
    final_content: Optional[str]
    status: ManagementRepLetterStatus
    prepared_by: str
    prepared_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]
    approved_by: Optional[str]
    approved_at: Optional[datetime]
    sent_to_client_at: Optional[datetime]
    received_signed_at: Optional[datetime]


class ManagementRepLetterEditIn(BaseModel):
    final_content: str = Field(..., min_length=1, max_length=50000)


class WorkingPaperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    version: int
    title: str
    sa_reference: str
    content: str
    cross_references: list[dict[str, Any]]
    source_snapshot: dict[str, Any]
    status: WorkingPaperStatus
    prepared_by: str
    prepared_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]
    approved_by: Optional[str]
    approved_at: Optional[datetime]


class WorkingPaperReviewIn(BaseModel):
    decision: str = Field(..., pattern=r"^(approve|rework)$")
    notes: Optional[str] = Field(None, max_length=4000)


class AuditProcedureCreateIn(BaseModel):
    sa_reference: str = Field(..., min_length=3, max_length=100)
    procedure_name: str = Field(..., min_length=3, max_length=300)


class AuditProcedurePerformIn(BaseModel):
    evidence_file_paths: list[str] = Field(default_factory=list, max_length=100)
    conclusion_notes: str = Field(..., min_length=1, max_length=12000)


class AuditProcedureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    sa_reference: str
    procedure_name: str
    status: AuditProcedureStatus
    performed_by: str
    performed_at: Optional[datetime]
    evidence_file_paths: list[str]
    conclusion_notes: Optional[str]
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    review_notes: Optional[str]
    approved_by: Optional[str]
    approved_at: Optional[datetime]


class TrialBalanceUploadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    period_type: TrialBalancePeriodType
    version: int
    is_current: bool
    compares_to_upload_id: Optional[str] = None
    original_filename: str
    file_sha256: str
    row_count: int
    uploaded_by: str
    uploaded_at: datetime


class TrialBalanceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    upload_id: str
    account_code: str
    account_name: str
    account_group: Optional[str] = None
    current_year_amount: Optional[Decimal] = None
    prior_year_amount: Optional[Decimal] = None
    variance_amount: Optional[Decimal] = None
    variance_pct: Optional[Decimal] = None
    source_row_number: Optional[int] = None


class TrialBalanceUploadResultOut(BaseModel):
    accepted: bool
    upload: Optional[TrialBalanceUploadOut] = None
    inserted: int = 0
    errors: list[RowError] = Field(default_factory=list)


class TrialBalanceCompareResultOut(BaseModel):
    accepted: bool
    current_upload: Optional[TrialBalanceUploadOut] = None
    prior_upload: Optional[TrialBalanceUploadOut] = None
    inserted: int = 0
    new_accounts: list[str] = Field(default_factory=list)
    prior_only_accounts: list[str] = Field(default_factory=list)
    risk_flags_generated: int = 0
    risk_flags_status: Optional[str] = None
    errors: list[RowError] = Field(default_factory=list)


class RiskFlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    trial_balance_line_id: Optional[str]
    source_reference_key: Optional[str] = None
    policy_version: Optional[int]
    flag_type: RiskFlagType
    severity: str
    system_note: str
    trigger_data: dict
    status: RiskFlagStatus
    assigned_to: Optional[str]
    resolution_notes: Optional[str]
    resolved_by: Optional[str]
    resolved_at: Optional[datetime]
    created_at: datetime


class RelatedPartyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    pan: Optional[str] = Field(None, max_length=10)
    relationship_nature: str = Field(..., min_length=1, max_length=2000)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None

    @field_validator("aliases")
    @classmethod
    def _clean_aliases(cls, values):
        cleaned = [value.strip() for value in values if value and value.strip()]
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("Aliases must be unique.")
        return cleaned

    @field_validator("pan")
    @classmethod
    def _normalize_related_party_pan(cls, value):
        return value.strip().upper() if value else None


class RelatedPartyUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=300)
    aliases: Optional[list[str]] = Field(None, max_length=30)
    pan: Optional[str] = Field(None, max_length=10)
    relationship_nature: Optional[str] = Field(None, min_length=1, max_length=2000)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    is_active: Optional[bool] = None

    @field_validator("aliases")
    @classmethod
    def _clean_update_aliases(cls, values):
        if values is None:
            return values
        cleaned = [value.strip() for value in values if value and value.strip()]
        if len({value.casefold() for value in cleaned}) != len(cleaned):
            raise ValueError("Aliases must be unique.")
        return cleaned

    @field_validator("pan")
    @classmethod
    def _normalize_update_pan(cls, value):
        return value.strip().upper() if value else None


class RelatedPartyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_id: str
    name: str
    aliases: list[str]
    pan: Optional[str]
    relationship_nature: str
    effective_from: Optional[date]
    effective_to: Optional[date]
    is_active: bool
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime


class RelatedPartyMatchResultOut(BaseModel):
    engagement_id: str
    matched_sources: int
    flags_created: int
    risk_flag_ids: list[str]


class StatutoryFilingRecordCreate(BaseModel):
    tax_type: StatutoryTaxType
    period: str = Field(..., pattern=r"^20\d{2}-(0[1-9]|1[0-2])$")
    filing_status: StatutoryFilingStatus
    filing_source: StatutoryFilingSource
    filed_at: Optional[date] = None
    statutory_due_date: Optional[date] = None
    amount_due: Optional[Decimal] = Field(None, ge=0)
    amount_paid: Optional[Decimal] = Field(None, ge=0)
    source_reference: Optional[str] = Field(None, max_length=500)
    notes: Optional[str] = Field(None, max_length=4000)


class StatutoryFilingRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    tax_type: StatutoryTaxType
    period: str
    version: int
    is_current: bool
    filing_status: StatutoryFilingStatus
    filing_source: StatutoryFilingSource
    filed_at: Optional[date]
    statutory_due_date: Optional[date]
    amount_due: Optional[Decimal]
    amount_paid: Optional[Decimal]
    source_reference: Optional[str]
    notes: Optional[str]
    recorded_by: str
    recorded_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]


class GSTR2BContextOut(BaseModel):
    gstin_id: str
    gstin: str
    gstr2b_available: bool
    gstr2b_fetched_at: Optional[datetime]
    reconciliation_available: bool
    scope_note: str


class StatutoryDuesPeriodOut(BaseModel):
    period: str
    tax_type: StatutoryTaxType
    filing_status: Optional[StatutoryFilingStatus]
    verification_status: str
    current_record: Optional[StatutoryFilingRecordOut]
    gstr2b_context: list[GSTR2BContextOut] = Field(default_factory=list)


class LedgerUploadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    version: int
    is_current: bool
    original_filename: str
    file_sha256: str
    row_count: int
    uploaded_by: str
    uploaded_at: datetime


class AgeingScheduleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    schedule_type: AgeingType
    generated_at: datetime
    as_of_date: date
    source_upload_id: str
    source_version: int
    data: dict[str, Any]
    generated_by: str


class LedgerUploadResultOut(BaseModel):
    accepted: bool
    upload: Optional[LedgerUploadOut] = None
    schedules: list[AgeingScheduleOut] = Field(default_factory=list)
    ageing_flags_generated: int = 0
    errors: list[RowError] = Field(default_factory=list)


class ClientBankAccountCreate(BaseModel):
    bank_name: str = Field(..., min_length=2, max_length=200)
    branch_name: Optional[str] = Field(None, max_length=200)
    account_name: Optional[str] = Field(None, max_length=200)
    account_number: str = Field(..., min_length=6, max_length=34, pattern=r"^[A-Za-z0-9]+$")
    ifsc_code: Optional[str] = Field(None, pattern=r"^[A-Za-z]{4}0[A-Za-z0-9]{6}$")


class ClientBankAccountUpdate(BaseModel):
    bank_name: Optional[str] = Field(None, min_length=2, max_length=200)
    branch_name: Optional[str] = Field(None, max_length=200)
    account_name: Optional[str] = Field(None, max_length=200)
    ifsc_code: Optional[str] = Field(None, pattern=r"^[A-Za-z]{4}0[A-Za-z0-9]{6}$")


class ClientBankAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    client_id: str
    bank_name: str
    branch_name: Optional[str]
    account_name: Optional[str]
    masked_account_number: str
    ifsc_code: Optional[str]
    is_active: bool
    created_by: str
    created_at: datetime
    updated_at: datetime


class BankConfirmationGenerateIn(BaseModel):
    as_of_date: date


class BankConfirmationReceiveIn(BaseModel):
    books_balance: Decimal = Field(..., ge=0)
    confirmed_balance: Decimal
    discrepancy_notes: Optional[str] = Field(None, max_length=4000)


class BankConfirmationReconcileIn(BaseModel):
    notes: Optional[str] = Field(None, min_length=10, max_length=4000)


class BankConfirmationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    bank_account_id: str
    account_number_last4: str
    version: int
    as_of_date: date
    letter_status: BankConfirmationStatus
    draft_content: str
    drafted_by: str
    drafted_at: datetime
    approved_by: Optional[str]
    approved_at: Optional[datetime]
    sent_by: Optional[str]
    sent_at: Optional[datetime]
    received_at: Optional[datetime]
    books_balance: Optional[Decimal]
    confirmed_balance: Optional[Decimal]
    discrepancy_amount: Optional[Decimal]
    discrepancy_notes: Optional[str]
    reconciled_by: Optional[str]
    reconciled_at: Optional[datetime]


class AuditRatioAnalysisCreate(BaseModel):
    current_assets_account_codes: list[str] = Field(..., min_length=1)
    inventory_account_codes: list[str] = Field(..., min_length=1)
    current_liabilities_account_codes: list[str] = Field(..., min_length=1)
    borrowings_account_codes: list[str] = Field(..., min_length=1)
    equity_account_codes: list[str] = Field(..., min_length=1)
    revenue_account_codes: list[str] = Field(..., min_length=1)
    cost_of_goods_sold_account_codes: list[str] = Field(..., min_length=1)
    credit_sales_account_codes: list[str] = Field(..., min_length=1)
    trade_receivables_account_codes: list[str] = Field(..., min_length=1)
    net_profit_account_codes: list[str] = Field(..., min_length=1)
    prior_opening_inventory: Optional[Decimal] = Field(None, ge=0)
    prior_opening_receivables: Optional[Decimal] = Field(None, ge=0)
    prior_opening_source_reference: Optional[str] = Field(None, max_length=500)

    @field_validator(
        "current_assets_account_codes", "inventory_account_codes",
        "current_liabilities_account_codes", "borrowings_account_codes",
        "equity_account_codes", "revenue_account_codes",
        "cost_of_goods_sold_account_codes", "credit_sales_account_codes",
        "trade_receivables_account_codes", "net_profit_account_codes",
    )
    @classmethod
    def _normalize_account_codes(cls, value):
        normalized = [str(code).strip() for code in value]
        if any(not code for code in normalized):
            raise ValueError("Account codes cannot be empty.")
        if len({code.casefold() for code in normalized}) != len(normalized):
            raise ValueError("An account code cannot appear twice in one ratio mapping.")
        return normalized

    @field_validator("prior_opening_source_reference")
    @classmethod
    def _prior_openings_need_reference(cls, value, info):
        supplied = info.data.get("prior_opening_inventory") is not None or info.data.get("prior_opening_receivables") is not None
        if supplied and not value:
            raise ValueError("A source reference is required for manually supplied prior-year opening balances.")
        return value


class AuditRatioAnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    engagement_id: str
    version: int
    status: RatioAnalysisStatus
    closing_upload_id: str
    opening_upload_id: Optional[str]
    prior_opening_source_reference: Optional[str]
    account_mapping: dict[str, Any]
    ratio_results: dict[str, Any]
    movement_threshold_pct: Decimal
    risk_flag_ids: list[str]
    prepared_by: str
    prepared_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    approved_by: Optional[str]
    approved_at: Optional[datetime]
