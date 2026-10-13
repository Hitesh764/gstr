"""
SQLAlchemy ORM models — the full multi-tenant schema.

Ownership chain
---------------
    Firm
     └── Client
          └── ClientGSTIN            (a business may hold one GSTIN per state)
               ├── PurchaseRegisterUpload  ──> PurchaseRegisterEntry
               ├── GSTR2BFetch             ──> GSTR2BEntry
               └── ReconciliationRun       ──> ReconciliationResult
                                                └── ReconciliationException
                                                     └── ExceptionComment

Two deliberate design decisions:

1. `firm_id` is denormalised onto every tenant-scoped table, even though it
   is derivable by walking the chain up to Firm. Entries sit four joins
   below the tenant root, and requiring a four-table join for every
   authorisation check would make row-level enforcement both slow and easy
   to forget. With the column present, `app/tenancy.py` applies one
   mandatory filter to every query. See tests/test_tenant_isolation.py.

2. Workflow state lives in `ReconciliationException`, NOT on
   `ReconciliationResult`. Results are derived data: re-running a
   reconciliation recomputes and replaces them. Resolution notes and
   approvals are human work and must survive a re-run, so they are stored
   separately and carried forward by `match_key`.

Enums are stored as VARCHAR with a CHECK constraint (`native_enum=False`,
`create_constraint=True`) rather than as PostgreSQL native enum types.
Adding a value to a native enum requires ALTER TYPE, which is awkward to run
and reverse in a migration.

The CHECK constraint is a real cost as well as a protection: adding a member
to any enum here needs a migration that replaces that column's constraint
(see alembic/versions/4da425ea355c for the pattern), otherwise the new value
is rejected by the database at runtime. SQLAlchemy 2.0 does NOT create the
constraint unless `create_constraint=True` is passed, which an earlier
version of this file failed to do while claiming otherwise.
"""
import enum
import uuid
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Column, String, Numeric, Date, DateTime, ForeignKey, Enum, Text, Integer,
    Float, Boolean, UniqueConstraint, Index, JSON,
)
from sqlalchemy.orm import relationship

from app.database import Base


def gen_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.utcnow()


def _enum(enum_cls, name):
    """String-backed enum column type (portable, migration-friendly)."""
    return Enum(enum_cls, name=name, native_enum=False, length=40,
                create_constraint=True,
                values_callable=lambda e: [m.value for m in e])


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    OWNER = "OWNER"                   # firm owner: full control including billing
    PARTNER = "PARTNER"               # partner: full control except billing
    MANAGER = "MANAGER"               # client management + approvals
    ASSOCIATE = "ASSOCIATE"           # data entry + reconciliation work
    CLIENT_VIEWER = "CLIENT_VIEWER"   # read-only, restricted to its own client


class SubscriptionTier(str, enum.Enum):
    STARTER = "STARTER"
    GROWTH = "GROWTH"
    ENTERPRISE = "ENTERPRISE"


class SubscriptionStatus(str, enum.Enum):
    TRIALING = "TRIALING"
    ACTIVE = "ACTIVE"
    PAST_DUE = "PAST_DUE"
    CANCELLED = "CANCELLED"


class ClientStatus(str, enum.Enum):
    ONBOARDING = "ONBOARDING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    OFFBOARDED = "OFFBOARDED"


class EntityType(str, enum.Enum):
    PROPRIETORSHIP = "PROPRIETORSHIP"
    PARTNERSHIP = "PARTNERSHIP"
    LLP = "LLP"
    COMPANY = "COMPANY"
    TRUST = "TRUST"
    OTHER = "OTHER"


class EngagementType(str, enum.Enum):
    COMPLIANCE_ONLY = "COMPLIANCE_ONLY"
    AUDIT = "AUDIT"
    BOTH = "BOTH"


class AuditEngagementType(str, enum.Enum):
    STATUTORY = "STATUTORY"
    TAX = "TAX"
    INTERNAL = "INTERNAL"


class AuditEngagementStatus(str, enum.Enum):
    PLANNING = "PLANNING"
    FIELDWORK = "FIELDWORK"
    COMPLETION = "COMPLETION"
    REPORTING = "REPORTING"
    CLOSED = "CLOSED"


class AuditPolicyStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"


class MaterialityBasis(str, enum.Enum):
    TURNOVER = "TURNOVER"
    PROFIT_BEFORE_TAX = "PROFIT_BEFORE_TAX"
    NET_WORTH = "NET_WORTH"


class TrialBalancePeriodType(str, enum.Enum):
    OPENING = "OPENING"
    CLOSING = "CLOSING"
    COMPARATIVE = "COMPARATIVE"


class RiskFlagType(str, enum.Enum):
    VARIANCE = "VARIANCE"
    RATIO = "RATIO"
    RELATED_PARTY = "RELATED_PARTY"
    STATUTORY_DUES = "STATUTORY_DUES"
    OTHER = "OTHER"


class RiskFlagStatus(str, enum.Enum):
    SYSTEM_FLAGGED = "SYSTEM_FLAGGED"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class StatutoryTaxType(str, enum.Enum):
    GST = "GST"
    TDS = "TDS"
    PF = "PF"
    ESI = "ESI"


class StatutoryFilingStatus(str, enum.Enum):
    FILED = "FILED"
    PENDING = "PENDING"
    ARREARS = "ARREARS"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class StatutoryFilingSource(str, enum.Enum):
    PORTAL_RECORD = "PORTAL_RECORD"
    CLIENT_RECORD = "CLIENT_RECORD"
    MANAGEMENT_REPRESENTATION = "MANAGEMENT_REPRESENTATION"
    OTHER = "OTHER"


class AgeingType(str, enum.Enum):
    DEBTORS = "DEBTORS"
    CREDITORS = "CREDITORS"


class BankConfirmationStatus(str, enum.Enum):
    DRAFTED = "DRAFTED"
    SENT = "SENT"
    PENDING = "PENDING"
    RECEIVED = "RECEIVED"
    RECONCILED = "RECONCILED"


class RatioAnalysisStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    MANAGER_REVIEWED = "MANAGER_REVIEWED"
    APPROVED = "APPROVED"


class CAROResponseStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    MANAGER_REVIEWED = "MANAGER_REVIEWED"
    FINALIZED = "FINALIZED"
    SUPERSEDED = "SUPERSEDED"


class ManagementRepLetterStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    MANAGER_REVIEWED = "MANAGER_REVIEWED"
    PARTNER_APPROVED = "PARTNER_APPROVED"
    SENT = "SENT"
    RECEIVED = "RECEIVED"


class WorkingPaperStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    MANAGER_REVIEWED = "MANAGER_REVIEWED"
    PARTNER_APPROVED = "PARTNER_APPROVED"


class AuditProcedureStatus(str, enum.Enum):
    PLANNED = "PLANNED"
    PERFORMED = "PERFORMED"
    MANAGER_REVIEWED = "MANAGER_REVIEWED"
    REWORK = "REWORK"
    PARTNER_APPROVED = "PARTNER_APPROVED"


class AuditAssessmentStatus(str, enum.Enum):
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    OVERRIDDEN = "OVERRIDDEN"
    SUPERSEDED = "SUPERSEDED"


class UploadStatus(str, enum.Enum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    IMPORTED = "IMPORTED"
    REJECTED = "REJECTED"


class GSTR1ReturnStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    EXPORTED = "EXPORTED"


class GSTR1InvoiceStatus(str, enum.Enum):
    SYSTEM_FLAGGED = "SYSTEM_FLAGGED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REVIEWED = "REVIEWED"


class FetchSource(str, enum.Enum):
    API = "API"
    MANUAL = "MANUAL"
    SAMPLE = "SAMPLE"


class RunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class MatchStatus(str, enum.Enum):
    MATCHED = "MATCHED"
    FORMATTING_MISMATCH = "FORMATTING_MISMATCH"
    SUPPLIER_NOT_FILED = "SUPPLIER_NOT_FILED"
    VALUE_MISMATCH = "VALUE_MISMATCH"
    CREDIT_NOTE_DISCREPANCY = "CREDIT_NOTE_DISCREPANCY"
    MISSING_IN_BOOKS = "MISSING_IN_BOOKS"


class ExceptionStatus(str, enum.Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REWORK = "REWORK"
    AUTO_RESOLVED = "AUTO_RESOLVED"


class NoticeStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESPONSE_DRAFTED = "RESPONSE_DRAFTED"
    PARTNER_APPROVED = "PARTNER_APPROVED"
    FILED = "FILED"
    CLOSED = "CLOSED"


class NoticeExtractionStatus(str, enum.Enum):
    PENDING = "PENDING"
    DONE = "DONE"             # text read from the file
    NEEDS_OCR = "NEEDS_OCR"   # a scan or photo, and OCR is not available
    FAILED = "FAILED"         # the file could not be read at all
    MANUAL = "MANUAL"         # text was typed or pasted in by a person


class NoticeAnalysisStatus(str, enum.Enum):
    NOT_RUN = "NOT_RUN"
    DONE = "DONE"
    FAILED = "FAILED"


class ConsentType(str, enum.Enum):
    GSP_GSTR_ACCESS = "GSP_GSTR_ACCESS"
    DATA_PROCESSING = "DATA_PROCESSING"


class ConsentAttemptStatus(str, enum.Enum):
    PENDING = "PENDING"       # OTP requested, awaiting entry
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"         # too many wrong OTPs, or the provider refused
    EXPIRED = "EXPIRED"       # the OTP window passed unused


class FetchAttemptStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    RETRYING = "RETRYING"     # a transient failure; another try is scheduled
    FAILED = "FAILED"         # final: retries exhausted or not worth retrying
    SKIPPED = "SKIPPED"       # nothing to do (already fetched, or not connected)


class FetchTrigger(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------

class TimestampMixin:
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class SoftDeleteMixin:
    """Records are flagged, never destroyed.

    `retention_until` records when a row becomes eligible for physical
    purge, so a firm can evidence its data-handling policy during audit
    (DPDP Act storage-limitation principle).
    """
    deleted_at = Column(DateTime, nullable=True, index=True)
    retention_until = Column(DateTime, nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


# ---------------------------------------------------------------------------
# Tenant root
# ---------------------------------------------------------------------------

class Firm(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "firms"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String(200), nullable=False)
    # A CA firm is itself a registered business.
    pan = Column(String(10), nullable=True)
    gstin = Column(String(15), nullable=True)
    contact_email = Column(String(255), nullable=False)
    contact_phone = Column(String(20), nullable=True)

    subscription_tier = Column(_enum(SubscriptionTier, "subscription_tier"),
                               nullable=False, default=SubscriptionTier.STARTER)
    subscription_status = Column(_enum(SubscriptionStatus, "subscription_status"),
                                 nullable=False, default=SubscriptionStatus.TRIALING)

    # Firm-level security policy
    require_2fa_for_partners = Column(Boolean, nullable=False, default=False)

    # Firm-configurable matching tolerances. NULL means "use the system
    # default" from app/config.py, so behaviour is unchanged until a firm
    # deliberately overrides it.
    exact_match_value_tolerance = Column(Numeric(10, 2), nullable=True)
    formatting_value_tolerance = Column(Numeric(10, 2), nullable=True)
    formatting_date_tolerance_days = Column(Integer, nullable=True)
    auto_resolve_confidence_threshold = Column(Integer, nullable=True)  # 0-100

    users = relationship("User", back_populates="firm",
                         foreign_keys="User.firm_id", cascade="all, delete-orphan")
    clients = relationship("Client", back_populates="firm", cascade="all, delete-orphan")


class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_firm_id_email", "firm_id", "email"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)

    name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False, index=True)
    phone = Column(String(20), nullable=True)
    role = Column(_enum(UserRole, "user_role"), nullable=False, default=UserRole.ASSOCIATE)

    password_hash = Column(String(255), nullable=False)
    totp_secret = Column(String(255), nullable=True)   # encrypted at rest
    totp_enabled = Column(Boolean, nullable=False, default=False)

    is_active = Column(Boolean, nullable=False, default=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    phone_verified = Column(Boolean, nullable=False, default=False)

    # Brute-force protection
    failed_login_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)

    # A CLIENT_VIEWER is restricted to exactly one client.
    #
    # users.scoped_client_id -> clients.id and clients.assigned_associate_id
    # -> users.id form a cycle. `use_alter` tells SQLAlchemy to add this
    # constraint after both tables exist rather than inline, which is what
    # lets PostgreSQL create the schema at all; without it the CREATE TABLE
    # ordering is unsatisfiable.
    scoped_client_id = Column(
        String,
        ForeignKey("clients.id", use_alter=True, name="fk_users_scoped_client_id_clients"),
        nullable=True,
    )

    firm = relationship("Firm", back_populates="users", foreign_keys=[firm_id])


class RefreshToken(Base):
    """Refresh tokens stored hashed, so a database leak cannot be replayed.

    Rows are retained (not deleted) on logout so that "log out all devices"
    and token-reuse detection both have history to work with.
    """
    __tablename__ = "refresh_tokens"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    issued_at = Column(DateTime, nullable=False, default=utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)
    user_agent = Column(String(400), nullable=True)
    ip_address = Column(String(64), nullable=True)


class Invitation(Base, TimestampMixin):
    """Invite-based staff onboarding: a firm owner invites by email."""
    __tablename__ = "invitations"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    email = Column(String(255), nullable=False, index=True)
    role = Column(_enum(UserRole, "invite_role"), nullable=False, default=UserRole.ASSOCIATE)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    invited_by = Column(String, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    accepted_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

class Client(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "clients"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)

    name = Column(String(200), nullable=False)
    pan = Column(String(10), nullable=True, index=True)
    entity_type = Column(_enum(EntityType, "entity_type"),
                         nullable=False, default=EntityType.OTHER)
    engagement_type = Column(_enum(EngagementType, "engagement_type"),
                             nullable=False, default=EngagementType.COMPLIANCE_ONLY)
    status = Column(_enum(ClientStatus, "client_status"),
                    nullable=False, default=ClientStatus.ONBOARDING)

    contact_name = Column(String(200), nullable=True)
    contact_email = Column(String(255), nullable=True)
    contact_phone = Column(String(20), nullable=True)

    assigned_associate_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    reconciliation_frequency = Column(String(20), nullable=False, default="MONTHLY")

    firm = relationship("Firm", back_populates="clients")
    gstins = relationship("ClientGSTIN", back_populates="client", cascade="all, delete-orphan")
    audit_engagements = relationship("AuditEngagement", back_populates="client")


class ClientGSTIN(Base, TimestampMixin, SoftDeleteMixin):
    """One GSTIN registration. A multi-state business holds several."""
    __tablename__ = "client_gstins"
    __table_args__ = (
        UniqueConstraint("firm_id", "gstin", name="uq_client_gstins_firm_gstin"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)

    gstin = Column(String(15), nullable=False, index=True)
    state_code = Column(String(2), nullable=True)
    state_name = Column(String(80), nullable=True)
    legal_name = Column(String(200), nullable=True)   # as returned by GSTIN verification
    trade_name = Column(String(200), nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    verified_at = Column(DateTime, nullable=True)

    # GSP consent. The token is encrypted at rest (see app/crypto.py).
    #
    # Two expiries, because a taxpayer token and a consent are different things:
    #   gsp_token_expires_at  when the CURRENT access token dies (6h for Sandbox).
    #                         Refreshed without an OTP while the session lives.
    #   consent_expires_at    when the whole SESSION ends and an OTP is needed
    #                         again. Chosen by the taxpayer on the GST portal
    #                         (6 hours to 30 days), so it is read from the
    #                         provider and never assumed.
    gsp_consent_token = Column(Text, nullable=True)
    consent_granted_at = Column(DateTime, nullable=True)
    consent_expires_at = Column(DateTime, nullable=True, index=True)
    gsp_token_expires_at = Column(DateTime, nullable=True)
    gsp_last_refreshed_at = Column(DateTime, nullable=True)
    gsp_provider = Column(String(50), nullable=True)   # who granted it ("sandbox", "mock")
    # The taxpayer's GST portal login, which the OTP is issued against.
    gst_portal_username = Column(String(100), nullable=True)

    client = relationship("Client", back_populates="gstins")


# ---------------------------------------------------------------------------
# Audit engagements
# ---------------------------------------------------------------------------

class AuditEngagement(Base, TimestampMixin):
    """A firm's audit engagement for one client, financial year and type.

    Statutory, tax and internal engagements can coexist for the same client
    and year, but duplicate engagements of the same type cannot be created.
    Workflow stages are declared here; approval gates are enforced by the
    workflow service as those review artifacts are implemented.
    """
    __tablename__ = "audit_engagements"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "financial_year", "engagement_type",
            name="uq_audit_engagement_client_year_type",
        ),
        Index("ix_audit_engagement_firm_status", "firm_id", "status"),
        Index("ix_audit_engagement_client_year", "client_id", "financial_year"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    financial_year = Column(String(7), nullable=False, index=True)  # e.g. 2025-26
    engagement_type = Column(
        _enum(AuditEngagementType, "audit_engagement_type"),
        nullable=False,
    )
    status = Column(
        _enum(AuditEngagementStatus, "audit_engagement_status"),
        nullable=False,
        default=AuditEngagementStatus.PLANNING,
    )
    engagement_partner_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)

    # Filled by later planning workflows. Nullable values mean “not yet
    # assessed”; false must never be inferred from a missing value.
    materiality_amount = Column(Numeric(18, 2), nullable=True)
    materiality_basis = Column(Text, nullable=True)
    caro_applicable = Column(Boolean, nullable=True)
    caro_applicability_reasoning = Column(Text, nullable=True)
    materiality_assessment_id = Column(String, nullable=True)
    materiality_confirmed_by = Column(String, ForeignKey("users.id"), nullable=True)
    materiality_confirmed_at = Column(DateTime, nullable=True)
    caro_assessment_id = Column(String, nullable=True)
    caro_confirmed_by = Column(String, ForeignKey("users.id"), nullable=True)
    caro_confirmed_at = Column(DateTime, nullable=True)
    reporting_deadline = Column(Date, nullable=True)

    client = relationship("Client", back_populates="audit_engagements")
    engagement_partner = relationship("User", foreign_keys=[engagement_partner_id])
    creator = relationship("User", foreign_keys=[created_by])


class FirmAuditPolicy(Base, TimestampMixin):
    """Versioned firm conventions for suggested audit analytics.

    A draft has no effect on calculations. A different Partner/Owner must
    approve it. Approval closes the previous approved policy's effective
    window, leaving a dated history of the conventions used by the firm.
    """
    __tablename__ = "firm_audit_policies"
    __table_args__ = (
        UniqueConstraint("firm_id", "version", name="uq_audit_policy_firm_version"),
        Index("ix_audit_policy_firm_effective", "firm_id", "effective_from", "effective_to"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(_enum(AuditPolicyStatus, "audit_policy_status"),
                    nullable=False, default=AuditPolicyStatus.DRAFT)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    materiality_default_basis = Column(
        _enum(MaterialityBasis, "materiality_basis"), nullable=False,
    )
    # Percentages are policy inputs, keyed by MaterialityBasis enum value.
    # They are intentionally not prescribed defaults: the firm supplies them.
    materiality_percentages = Column(JSON, nullable=False)
    variance_threshold_pct = Column(Numeric(7, 2), nullable=False)
    variance_materiality_multiplier = Column(Numeric(7, 2), nullable=False)
    ratio_movement_threshold_pct = Column(Numeric(7, 2), nullable=False)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)

    creator = relationship("User", foreign_keys=[created_by])
    approver = relationship("User", foreign_keys=[approved_by])


class AuditStandardReference(Base):
    """Versioned, platform-wide reference metadata for an ICAI SA."""
    __tablename__ = "audit_standard_references"
    __table_args__ = (
        UniqueConstraint("sa_number", "version", name="uq_audit_standard_number_version"),
        Index("ix_audit_standard_effective", "sa_number", "effective_from", "effective_to"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    sa_number = Column(String(20), nullable=False, index=True)
    title = Column(String(300), nullable=False)
    summary = Column(Text, nullable=False)
    version = Column(Integer, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    source_reference = Column(String(1000), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class CAROClauseDefinition(Base):
    """Versioned CARO clause text and structured applicability metadata."""
    __tablename__ = "caro_clause_definitions"
    __table_args__ = (
        UniqueConstraint("clause_number", "version", name="uq_caro_clause_number_version"),
        Index("ix_caro_clause_effective", "clause_number", "effective_from", "effective_to"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    clause_number = Column(Integer, nullable=False, index=True)
    title = Column(String(300), nullable=False)
    clause_text = Column(Text, nullable=False)
    applicability_criteria = Column(JSON, nullable=False)
    version = Column(Integer, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    source_reference = Column(String(1000), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class CAROApplicabilityRule(Base):
    """Versioned order-level applicability criteria, loaded from legal sources."""
    __tablename__ = "caro_applicability_rules"
    __table_args__ = (
        UniqueConstraint("version", name="uq_caro_applicability_rule_version"),
        Index("ix_caro_applicability_effective", "effective_from", "effective_to"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    version = Column(Integer, nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    rule_data = Column(JSON, nullable=False)
    source_reference = Column(String(1000), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class MaterialityAssessment(Base):
    """Immutable inputs and suggestion, followed by a recorded partner decision."""
    __tablename__ = "materiality_assessments"
    __table_args__ = (
        UniqueConstraint("engagement_id", "version", name="uq_materiality_assessment_eng_version"),
        Index("ix_materiality_assessment_engagement", "firm_id", "engagement_id", "version"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    firm_policy_id = Column(String, ForeignKey("firm_audit_policies.id"), nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(_enum(AuditAssessmentStatus, "audit_assessment_status"),
                    nullable=False, default=AuditAssessmentStatus.PROPOSED)
    basis = Column(_enum(MaterialityBasis, "materiality_assessment_basis"), nullable=False)
    basis_amount = Column(Numeric(18, 2), nullable=False)
    percentage = Column(Numeric(9, 4), nullable=False)
    suggested_amount = Column(Numeric(18, 2), nullable=False)
    inputs = Column(JSON, nullable=False)
    basis_rationale = Column(Text, nullable=True)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    confirmed_amount = Column(Numeric(18, 2), nullable=True)
    confirmed_by = Column(String, ForeignKey("users.id"), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    override_rationale = Column(Text, nullable=True)


class CAROApplicabilityAssessment(Base):
    """Rule output and partner confirmation for one engagement's CARO scope."""
    __tablename__ = "caro_applicability_assessments"
    __table_args__ = (
        UniqueConstraint("engagement_id", "version", name="uq_caro_assessment_eng_version"),
        Index("ix_caro_assessment_engagement", "firm_id", "engagement_id", "version"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    rule_id = Column(String, ForeignKey("caro_applicability_rules.id"), nullable=False)
    version = Column(Integer, nullable=False)
    status = Column(_enum(AuditAssessmentStatus, "caro_assessment_status"),
                    nullable=False, default=AuditAssessmentStatus.PROPOSED)
    determined_applicable = Column(Boolean, nullable=False)
    applicable_clause_numbers = Column(JSON, nullable=False)
    reasoning = Column(Text, nullable=False)
    inputs = Column(JSON, nullable=False)
    assessed_by = Column(String, ForeignKey("users.id"), nullable=False)
    assessed_at = Column(DateTime, nullable=False, default=utcnow)
    confirmed_applicable = Column(Boolean, nullable=True)
    confirmed_by = Column(String, ForeignKey("users.id"), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    confirmation_rationale = Column(Text, nullable=True)


class CAROClauseResponse(Base):
    """Version-pinned clause draft and human-owned final narrative."""
    __tablename__ = "caro_clause_responses"
    __table_args__ = (
        UniqueConstraint("engagement_id", "clause_number", "version",
                         name="uq_caro_response_eng_clause_version"),
        Index("ix_caro_response_eng_status", "firm_id", "engagement_id", "status"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    clause_definition_id = Column(String, ForeignKey("caro_clause_definitions.id"), nullable=False)
    clause_number = Column(Integer, nullable=False)
    version = Column(Integer, nullable=False)
    applicability = Column(String(30), nullable=False)
    applicability_reasoning = Column(Text, nullable=False)
    draft_narrative = Column(Text, nullable=False)
    final_narrative = Column(Text, nullable=True)
    source_snapshot = Column(JSON, nullable=False)
    status = Column(_enum(CAROResponseStatus, "caro_response_status"), nullable=False,
                    default=CAROResponseStatus.DRAFT)
    prepared_by = Column(String, ForeignKey("users.id"), nullable=False)
    prepared_at = Column(DateTime, nullable=False, default=utcnow)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)
    finalized_by = Column(String, ForeignKey("users.id"), nullable=True)
    finalized_at = Column(DateTime, nullable=True)


class AuditAIRecord(Base):
    """Exact prompt/response record for reproducibility of generated suggestions."""
    __tablename__ = "audit_ai_records"
    __table_args__ = (
        Index("ix_audit_ai_records_engagement", "firm_id", "engagement_id", "created_at"),
        Index("ix_audit_ai_records_entity", "entity_type", "entity_id"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    entity_type = Column(String(60), nullable=False)
    entity_id = Column(String, nullable=False)
    attempt_number = Column(Integer, nullable=False, default=1)
    provider = Column(String(40), nullable=False)
    model = Column(String(200), nullable=False)
    exact_prompt = Column(Text, nullable=False)
    exact_response = Column(Text, nullable=False)
    response_sha256 = Column(String(64), nullable=False)
    accepted = Column(Boolean, nullable=False)
    generated_by = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class ManagementRepLetter(Base):
    """SA 580 representation letter with a human approval and send trail."""
    __tablename__ = "management_rep_letters"
    __table_args__ = (
        UniqueConstraint("engagement_id", "version", name="uq_mrl_engagement_version"),
        Index("ix_mrl_engagement_status", "firm_id", "engagement_id", "status"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    template_version = Column(String(80), nullable=False)
    draft_content = Column(Text, nullable=False)
    final_content = Column(Text, nullable=True)
    status = Column(_enum(ManagementRepLetterStatus, "management_rep_letter_status"),
                    nullable=False, default=ManagementRepLetterStatus.DRAFT)
    prepared_by = Column(String, ForeignKey("users.id"), nullable=False)
    prepared_at = Column(DateTime, nullable=False, default=utcnow)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    sent_to_client_at = Column(DateTime, nullable=True)
    received_signed_at = Column(DateTime, nullable=True)


class WorkingPaper(Base):
    """Compiled evidence and references, subject to manager and partner approval."""
    __tablename__ = "audit_working_papers"
    __table_args__ = (
        UniqueConstraint("engagement_id", "version", name="uq_working_paper_engagement_version"),
        Index("ix_working_paper_engagement_status", "firm_id", "engagement_id", "status"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    title = Column(String(300), nullable=False)
    sa_reference = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    cross_references = Column(JSON, nullable=False)
    source_snapshot = Column(JSON, nullable=False)
    status = Column(_enum(WorkingPaperStatus, "working_paper_status"), nullable=False,
                    default=WorkingPaperStatus.DRAFT)
    prepared_by = Column(String, ForeignKey("users.id"), nullable=False)
    prepared_at = Column(DateTime, nullable=False, default=utcnow)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)


class AuditProcedure(Base):
    """Human-performed audit procedure and its independent review trail."""
    __tablename__ = "audit_procedures"
    __table_args__ = (
        Index("ix_audit_procedures_engagement_status", "firm_id", "engagement_id", "status"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    sa_reference = Column(String(100), nullable=False)
    procedure_name = Column(String(300), nullable=False)
    status = Column(_enum(AuditProcedureStatus, "audit_procedure_status"), nullable=False,
                    default=AuditProcedureStatus.PLANNED)
    performed_by = Column(String, ForeignKey("users.id"), nullable=False)
    performed_at = Column(DateTime, nullable=True)
    evidence_file_paths = Column(JSON, nullable=False, default=list)
    conclusion_notes = Column(Text, nullable=True)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)


class TrialBalanceUpload(Base):
    """Encrypted source file and immutable version of a trial balance."""
    __tablename__ = "trial_balance_uploads"
    __table_args__ = (
        UniqueConstraint(
            "engagement_id", "period_type", "version",
            name="uq_trial_balance_engagement_type_version",
        ),
        Index("ix_trial_balance_current", "engagement_id", "period_type", "is_current"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    period_type = Column(
        _enum(TrialBalancePeriodType, "trial_balance_period_type"), nullable=False,
    )
    version = Column(Integer, nullable=False)
    is_current = Column(Boolean, nullable=False, default=True)
    compares_to_upload_id = Column(String, ForeignKey("trial_balance_uploads.id"), nullable=True)
    original_filename = Column(String(400), nullable=False)
    file_path = Column(String(1000), nullable=False)  # encrypted storage key
    file_sha256 = Column(String(64), nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=utcnow)

    lines = relationship("TrialBalanceLine", back_populates="upload",
                         cascade="all, delete-orphan")
    compares_to = relationship("TrialBalanceUpload", remote_side=[id], uselist=False)


class TrialBalanceLine(Base):
    """Normalized account row; variances exist only when both years exist."""
    __tablename__ = "trial_balance_lines"
    __table_args__ = (
        UniqueConstraint("upload_id", "account_code", name="uq_trial_balance_line_account"),
        Index("ix_trial_balance_line_account", "firm_id", "account_code"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    upload_id = Column(String, ForeignKey("trial_balance_uploads.id"), nullable=False, index=True)
    account_code = Column(String(100), nullable=False)
    account_name = Column(String(300), nullable=False)
    account_group = Column(String(120), nullable=True)
    current_year_amount = Column(Numeric(18, 2), nullable=True)
    prior_year_amount = Column(Numeric(18, 2), nullable=True)
    variance_amount = Column(Numeric(18, 2), nullable=True)
    variance_pct = Column(Numeric(12, 4), nullable=True)
    source_row_number = Column(Integer, nullable=True)

    upload = relationship("TrialBalanceUpload", back_populates="lines")


class RiskFlag(Base):
    """System generated audit observation; never a concluded audit finding."""
    __tablename__ = "risk_flags"
    __table_args__ = (
        Index("ix_risk_flag_engagement_status", "firm_id", "engagement_id", "status"),
        Index("ix_risk_flag_trial_balance_line", "trial_balance_line_id"),
        Index("uq_risk_flag_source_reference_key", "source_reference_key", unique=True),
        UniqueConstraint("engagement_id", "trial_balance_line_id", "flag_type", "policy_version",
                         name="uq_risk_flag_line_type_policy_version"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    trial_balance_line_id = Column(String, ForeignKey("trial_balance_lines.id"), nullable=True)
    source_reference_key = Column(String(300), nullable=True)
    policy_version = Column(Integer, nullable=True)
    flag_type = Column(_enum(RiskFlagType, "risk_flag_type"), nullable=False)
    severity = Column(String(40), nullable=False, default="REVIEW_REQUIRED")
    system_note = Column(Text, nullable=False)
    trigger_data = Column(JSON, nullable=False)
    status = Column(_enum(RiskFlagStatus, "risk_flag_status"), nullable=False,
                    default=RiskFlagStatus.SYSTEM_FLAGGED)
    assigned_to = Column(String, ForeignKey("users.id"), nullable=True)
    resolution_notes = Column(Text, nullable=True)
    resolved_by = Column(String, ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class RelatedParty(Base):
    """Client-maintained related-party master; matches always require review."""
    __tablename__ = "client_related_parties"
    __table_args__ = (
        UniqueConstraint("client_id", "normalized_name", name="uq_client_related_party_name"),
        Index("ix_related_parties_client_active", "firm_id", "client_id", "is_active"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    name = Column(String(300), nullable=False)
    normalized_name = Column(String(300), nullable=False)
    aliases = Column(JSON, nullable=False, default=list)
    pan = Column(String(10), nullable=True)
    relationship_nature = Column(Text, nullable=False)
    effective_from = Column(Date, nullable=True)
    effective_to = Column(Date, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    updated_by = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class StatutoryFilingRecord(Base):
    """Versioned, source-attributed filing status reported for audit testing."""
    __tablename__ = "statutory_filing_records"
    __table_args__ = (
        UniqueConstraint("engagement_id", "tax_type", "period", "version",
                         name="uq_statutory_filing_eng_tax_period_version"),
        Index("ix_statutory_filing_eng_period", "firm_id", "engagement_id", "period"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    tax_type = Column(_enum(StatutoryTaxType, "statutory_tax_type"), nullable=False)
    period = Column(String(7), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    is_current = Column(Boolean, nullable=False, default=True)
    filing_status = Column(_enum(StatutoryFilingStatus, "statutory_filing_status"), nullable=False)
    filing_source = Column(_enum(StatutoryFilingSource, "statutory_filing_source"), nullable=False)
    filed_at = Column(Date, nullable=True)
    statutory_due_date = Column(Date, nullable=True)
    amount_due = Column(Numeric(18, 2), nullable=True)
    amount_paid = Column(Numeric(18, 2), nullable=True)
    source_reference = Column(String(500), nullable=True)
    notes = Column(Text, nullable=True)
    recorded_by = Column(String, ForeignKey("users.id"), nullable=False)
    recorded_at = Column(DateTime, nullable=False, default=utcnow)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)


class LedgerUpload(Base):
    """Immutable, encrypted invoice-level ledger source version."""
    __tablename__ = "audit_ledger_uploads"
    __table_args__ = (
        UniqueConstraint("engagement_id", "version", name="uq_audit_ledger_engagement_version"),
        Index("ix_audit_ledger_current", "engagement_id", "is_current"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    is_current = Column(Boolean, nullable=False, default=True)
    original_filename = Column(String(400), nullable=False)
    file_path = Column(String(1000), nullable=False)
    file_sha256 = Column(String(64), nullable=False)
    row_count = Column(Integer, nullable=False)
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=utcnow)


class LedgerLine(Base):
    __tablename__ = "audit_ledger_lines"
    __table_args__ = (
        Index("ix_audit_ledger_line_source_type", "firm_id", "upload_id", "account_type"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    upload_id = Column(String, ForeignKey("audit_ledger_uploads.id"), nullable=False, index=True)
    account_type = Column(_enum(AgeingType, "audit_ageing_type"), nullable=False)
    account_code = Column(String(100), nullable=True)
    account_name = Column(String(300), nullable=True)
    party_name = Column(String(300), nullable=False)
    invoice_number = Column(String(120), nullable=False)
    invoice_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=True)
    open_balance = Column(Numeric(18, 2), nullable=False)
    explanation = Column(Text, nullable=True)
    source_row_number = Column(Integer, nullable=False)


class AgeingSchedule(Base):
    __tablename__ = "audit_ageing_schedules"
    __table_args__ = (
        Index("ix_audit_ageing_engagement", "firm_id", "engagement_id", "generated_at"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    schedule_type = Column(_enum(AgeingType, "audit_ageing_schedule_type"), nullable=False)
    generated_at = Column(DateTime, nullable=False, default=utcnow)
    as_of_date = Column(Date, nullable=False)
    source_upload_id = Column(String, ForeignKey("audit_ledger_uploads.id"), nullable=False)
    source_version = Column(Integer, nullable=False)
    data = Column(JSON, nullable=False)
    generated_by = Column(String, ForeignKey("users.id"), nullable=False)


class ClientBankAccount(Base):
    """Client bank-account master; account number is encrypted and masked."""
    __tablename__ = "client_bank_accounts"
    __table_args__ = (
        UniqueConstraint("client_id", "account_number_blind_index", name="uq_client_bank_account_fingerprint"),
        Index("ix_client_bank_accounts_client_active", "firm_id", "client_id", "is_active"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    bank_name = Column(String(200), nullable=False)
    branch_name = Column(String(200), nullable=True)
    account_name = Column(String(200), nullable=True)
    account_number_encrypted = Column(Text, nullable=False)
    account_number_last4 = Column(String(4), nullable=False)
    account_number_blind_index = Column(String(64), nullable=False, index=True)
    ifsc_code = Column(String(11), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class BankConfirmation(Base):
    """Drafted confirmation letter and explicit approval/send/receipt trail."""
    __tablename__ = "bank_confirmations"
    __table_args__ = (
        UniqueConstraint("engagement_id", "bank_account_id", "as_of_date", "version",
                         name="uq_bank_confirmation_eng_account_date_version"),
        Index("ix_bank_confirmation_engagement_status", "firm_id", "engagement_id", "letter_status"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    bank_account_id = Column(String, ForeignKey("client_bank_accounts.id"), nullable=False, index=True)
    account_number_last4 = Column(String(4), nullable=False)
    version = Column(Integer, nullable=False)
    as_of_date = Column(Date, nullable=False)
    letter_status = Column(_enum(BankConfirmationStatus, "bank_confirmation_status"), nullable=False,
                           default=BankConfirmationStatus.DRAFTED)
    draft_content = Column(Text, nullable=False)
    drafted_by = Column(String, ForeignKey("users.id"), nullable=False)
    drafted_at = Column(DateTime, nullable=False, default=utcnow)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    sent_by = Column(String, ForeignKey("users.id"), nullable=True)
    sent_at = Column(DateTime, nullable=True)
    received_at = Column(DateTime, nullable=True)
    books_balance = Column(Numeric(18, 2), nullable=True)
    confirmed_balance = Column(Numeric(18, 2), nullable=True)
    discrepancy_amount = Column(Numeric(18, 2), nullable=True)
    discrepancy_notes = Column(Text, nullable=True)
    reconciled_by = Column(String, ForeignKey("users.id"), nullable=True)
    reconciled_at = Column(DateTime, nullable=True)


class AuditRatioAnalysis(Base):
    """Mechanical ratio output with reviewer and partner approval trail."""
    __tablename__ = "audit_ratio_analyses"
    __table_args__ = (
        UniqueConstraint("engagement_id", "version", name="uq_audit_ratio_analysis_engagement_version"),
        Index("ix_audit_ratio_analysis_engagement", "firm_id", "engagement_id", "version"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    engagement_id = Column(String, ForeignKey("audit_engagements.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(_enum(RatioAnalysisStatus, "audit_ratio_analysis_status"), nullable=False,
                    default=RatioAnalysisStatus.DRAFT)
    closing_upload_id = Column(String, ForeignKey("trial_balance_uploads.id"), nullable=False)
    opening_upload_id = Column(String, ForeignKey("trial_balance_uploads.id"), nullable=True)
    prior_opening_source_reference = Column(String(500), nullable=True)
    account_mapping = Column(JSON, nullable=False)
    ratio_results = Column(JSON, nullable=False)
    movement_threshold_pct = Column(Numeric(7, 2), nullable=False)
    risk_flag_ids = Column(JSON, nullable=False)
    prepared_by = Column(String, ForeignKey("users.id"), nullable=False)
    prepared_at = Column(DateTime, nullable=False, default=utcnow)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Purchase register ingestion (versioned)
# ---------------------------------------------------------------------------

class PurchaseRegisterUpload(Base, SoftDeleteMixin):
    """One upload of a purchase register for a GSTIN and period.

    Re-uploading the same period creates a NEW row with an incremented
    `version` rather than overwriting. `is_current` marks which version
    reconciliation uses, so history is preserved and diffable.
    """
    __tablename__ = "purchase_register_uploads"
    __table_args__ = (
        UniqueConstraint("client_gstin_id", "period", "version",
                         name="uq_pr_upload_gstin_period_version"),
        Index("ix_pr_upload_current", "client_gstin_id", "period", "is_current"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=False, index=True)

    period = Column(String(7), nullable=False, index=True)   # "2026-03"
    version = Column(Integer, nullable=False, default=1)
    is_current = Column(Boolean, nullable=False, default=True)

    source = Column(String(30), nullable=False, default="MANUAL_UPLOAD")  # MANUAL_UPLOAD / TALLY / ZOHO
    original_filename = Column(String(400), nullable=True)
    file_path = Column(String(1000), nullable=True)   # storage key, not local disk in production
    status = Column(_enum(UploadStatus, "upload_status"),
                    nullable=False, default=UploadStatus.PENDING)

    row_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    validation_errors = Column(JSON, nullable=True)  # per-row errors, shown before import is confirmed

    uploaded_by = Column(String, ForeignKey("users.id"), nullable=True)
    uploaded_at = Column(DateTime, nullable=False, default=utcnow)

    entries = relationship("PurchaseRegisterEntry", back_populates="upload",
                           cascade="all, delete-orphan")


class PurchaseRegisterEntry(Base):
    __tablename__ = "purchase_register_entries"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    upload_id = Column(String, ForeignKey("purchase_register_uploads.id"),
                       nullable=False, index=True)

    vendor_name = Column(String(300), nullable=False)
    vendor_gstin = Column(String(15), nullable=False, index=True)
    invoice_number = Column(String(100), nullable=False)
    invoice_date = Column(Date, nullable=False)
    taxable_value = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    cgst = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    sgst = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    igst = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    total_value = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    source_row_number = Column(Integer, nullable=True)   # row number in the uploaded file

    upload = relationship("PurchaseRegisterUpload", back_populates="entries")


# ---------------------------------------------------------------------------
# GSTR-1 outward supply preparation (separate from inward purchase data)
# ---------------------------------------------------------------------------

class GSTR1Return(Base, TimestampMixin, SoftDeleteMixin):
    """A draft outward-supplies return for one registration and tax period."""
    __tablename__ = "gstr1_returns"
    __table_args__ = (
        UniqueConstraint("client_gstin_id", "period", name="uq_gstr1_return_gstin_period"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=False, index=True)
    period = Column(String(7), nullable=False, index=True)
    status = Column(_enum(GSTR1ReturnStatus, "gstr1_return_status"), nullable=False,
                    default=GSTR1ReturnStatus.DRAFT)
    rule_version = Column(String(40), nullable=False, default="GST_PORTAL_2026-10")
    reconciliation_result = Column(JSON, nullable=True)
    source_upload_id = Column(String, ForeignKey("gstr1_sales_uploads.id"), nullable=True)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)


class GSTR1SalesUpload(Base, SoftDeleteMixin):
    __tablename__ = "gstr1_sales_uploads"
    __table_args__ = (UniqueConstraint("client_gstin_id", "period", "version",
                                       name="uq_gstr1_sales_upload_version"),)

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=False, index=True)
    period = Column(String(7), nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    is_current = Column(Boolean, nullable=False, default=True)
    original_filename = Column(String(400), nullable=True)
    file_path = Column(String(1000), nullable=True)
    source_sha256 = Column(String(64), nullable=False)
    status = Column(_enum(UploadStatus, "upload_status"), nullable=False, default=UploadStatus.IMPORTED)
    row_count = Column(Integer, nullable=False, default=0)
    issue_count = Column(Integer, nullable=False, default=0)
    rate_reference_snapshot = Column(JSON, nullable=True)
    normalization_snapshot = Column(JSON, nullable=True)
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=utcnow)


class GSTR1ClientFormatProfile(Base):
    """Private per-client memory for successfully resolved source layouts."""
    __tablename__ = "gstr1_client_format_profiles"
    __table_args__ = (UniqueConstraint("client_gstin_id", "source_type", "fingerprint",
                                       name="uq_gstr1_client_format_fingerprint"),)

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = Column(String(20), nullable=False)
    source_name = Column(String(100), nullable=False, default="Sales Register")
    fingerprint = Column(String(64), nullable=False, index=True)
    source_columns = Column(JSON, nullable=False)
    column_rules = Column(JSON, nullable=False)
    confirmed_columns = Column(JSON, nullable=False, default=dict)
    alias_version = Column(String(60), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    success_count = Column(Integer, nullable=False, default=0)
    import_count = Column(Integer, nullable=False, default=0)
    last_used_at = Column(DateTime, nullable=False, default=utcnow)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class GSTR1SalesInvoice(Base):
    __tablename__ = "gstr1_sales_invoices"
    __table_args__ = (Index("ix_gstr1_invoice_upload", "firm_id", "upload_id"),)

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    upload_id = Column(String, ForeignKey("gstr1_sales_uploads.id", ondelete="CASCADE"), nullable=False)
    row_number = Column(Integer, nullable=False)
    customer_name = Column(String(300), nullable=True)
    customer_gstin = Column(String(15), nullable=True, index=True)
    invoice_number = Column(String(100), nullable=False)
    normalized_invoice_number = Column(String(100), nullable=False, index=True)
    document_series = Column(String(100), nullable=True, index=True)
    invoice_date = Column(Date, nullable=False)
    invoice_value = Column(Numeric(16, 2), nullable=False)
    taxable_value = Column(Numeric(16, 2), nullable=False)
    cgst = Column(Numeric(16, 2), nullable=False, default=Decimal("0"))
    sgst = Column(Numeric(16, 2), nullable=False, default=Decimal("0"))
    igst = Column(Numeric(16, 2), nullable=False, default=Decimal("0"))
    cess = Column(Numeric(16, 2), nullable=False, default=Decimal("0"))
    gst_rate = Column(Numeric(7, 3), nullable=True)
    place_of_supply = Column(String(2), nullable=True)
    hsn_sac = Column(String(16), nullable=True)
    description = Column(String(500), nullable=True)
    original_invoice_number = Column(String(100), nullable=True)
    original_invoice_date = Column(Date, nullable=True)
    note_reason = Column(String(300), nullable=True)
    quantity = Column(Numeric(16, 3), nullable=False, default=Decimal("0"))
    uqc = Column(String(30), nullable=True)
    export_type = Column(String(80), nullable=True)
    shipping_bill_number = Column(String(100), nullable=True)
    shipping_bill_date = Column(Date, nullable=True)
    port_code = Column(String(30), nullable=True)
    invoice_status = Column(String(30), nullable=False, default="ACTIVE")
    supply_type = Column(String(30), nullable=False, default="GOODS")
    document_type = Column(String(30), nullable=False, default="INVOICE")
    classification = Column(String(30), nullable=False)
    classification_reason = Column(String(250), nullable=False)
    status = Column(_enum(GSTR1InvoiceStatus, "gstr1_invoice_status"), nullable=False,
                    default=GSTR1InvoiceStatus.NEEDS_REVIEW)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)


class GSTR1ValidationIssue(Base):
    __tablename__ = "gstr1_validation_issues"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    upload_id = Column(String, ForeignKey("gstr1_sales_uploads.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_id = Column(String, ForeignKey("gstr1_sales_invoices.id", ondelete="CASCADE"), nullable=True, index=True)
    row_number = Column(Integer, nullable=True)
    code = Column(String(60), nullable=False)
    severity = Column(String(20), nullable=False)
    message = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="OPEN")
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_decision = Column(String(30), nullable=True)
    review_note = Column(Text, nullable=True)


class GSTR1RuleDefinition(Base):
    """Effective-dated, reviewable system rules; tenant data is never altered."""
    __tablename__ = "gstr1_rule_definitions"
    __table_args__ = (UniqueConstraint("rule_key", "effective_from", name="uq_gstr1_rule_version"),)

    id = Column(String, primary_key=True, default=gen_id)
    rule_key = Column(String(80), nullable=False, index=True)
    version = Column(String(40), nullable=False)
    definition = Column(JSON, nullable=False)
    source_url = Column(String(500), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    verified_at = Column(DateTime, nullable=False, default=utcnow)


class GSTR1TenantRateReference(Base):
    """Firm-maintained, source-attributed GST-rate reference versions."""
    __tablename__ = "gstr1_tenant_rate_references"
    __table_args__ = (UniqueConstraint("firm_id", "version", name="uq_gstr1_tenant_rate_reference_version"),)

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    version = Column(String(60), nullable=False)
    rates_by_hsn = Column(JSON, nullable=False)
    source_url = Column(String(500), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=False)
    uploaded_at = Column(DateTime, nullable=False, default=utcnow)
    is_current = Column(Boolean, nullable=False, default=True)


class GSTR1DocumentIssued(Base):
    """Versioned source schedule for GSTR-1 Table 13 (documents issued)."""
    __tablename__ = "gstr1_documents_issued"
    __table_args__ = (UniqueConstraint("return_id", "document_type", "series_from", "version",
                                       name="uq_gstr1_document_series_version"),)

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    return_id = Column(String, ForeignKey("gstr1_returns.id", ondelete="CASCADE"), nullable=False, index=True)
    document_type = Column(String(40), nullable=False)
    series_from = Column(String(100), nullable=False)
    series_to = Column(String(100), nullable=False)
    issued_count = Column(Integer, nullable=False)
    cancelled_count = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    is_current = Column(Boolean, nullable=False, default=True)
    prepared_by = Column(String, ForeignKey("users.id"), nullable=False)
    prepared_at = Column(DateTime, nullable=False, default=utcnow)
    reviewed_by = Column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# GSTR-2B ingestion (versioned by fetch)
# ---------------------------------------------------------------------------

class GSTR2BFetch(Base, SoftDeleteMixin):
    __tablename__ = "gstr2b_fetches"
    __table_args__ = (
        Index("ix_gstr2b_fetch_current", "client_gstin_id", "period", "is_current"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=False, index=True)

    period = Column(String(7), nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    is_current = Column(Boolean, nullable=False, default=True)

    source = Column(_enum(FetchSource, "fetch_source"), nullable=False, default=FetchSource.MANUAL)
    gsp_provider = Column(String(50), nullable=True)   # "sandbox", "cleartax", ...
    generated_on = Column(Date, nullable=True)         # GSTR-2B generation date (the 14th)
    raw_payload = Column(JSON, nullable=True)          # exact GSP response, retained for audit
    fetched_at = Column(DateTime, nullable=False, default=utcnow)
    fetched_by = Column(String, ForeignKey("users.id"), nullable=True)

    entries = relationship("GSTR2BEntry", back_populates="fetch", cascade="all, delete-orphan")


class GSTR2BEntry(Base):
    __tablename__ = "gstr2b_entries"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    fetch_id = Column(String, ForeignKey("gstr2b_fetches.id"), nullable=False, index=True)

    vendor_name = Column(String(300), nullable=True)
    vendor_gstin = Column(String(15), nullable=False, index=True)
    invoice_number = Column(String(100), nullable=False)
    invoice_date = Column(Date, nullable=False)
    taxable_value = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    cgst = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    sgst = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    igst = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    total_value = Column(Numeric(14, 2), nullable=False, default=Decimal("0.00"))
    filing_status = Column(String(20), nullable=False, default="FILED")
    itc_available = Column(Boolean, nullable=False, default=True)

    fetch = relationship("GSTR2BFetch", back_populates="entries")


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class ReconciliationRun(Base, SoftDeleteMixin):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        Index("ix_recon_run_current", "client_gstin_id", "period", "is_current"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=False, index=True)

    period = Column(String(7), nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    is_current = Column(Boolean, nullable=False, default=True)

    # Exactly which inputs produced this run — essential for re-reconciliation
    # diffs and for defending a figure during audit.
    upload_id = Column(String, ForeignKey("purchase_register_uploads.id"), nullable=True)
    fetch_id = Column(String, ForeignKey("gstr2b_fetches.id"), nullable=True)

    status = Column(_enum(RunStatus, "run_status"), nullable=False, default=RunStatus.RUNNING)
    run_at = Column(DateTime, nullable=False, default=utcnow)
    run_by = Column(String, ForeignKey("users.id"), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)

    # Tolerances actually used, snapshotted so a historical run stays
    # explicable after the firm changes its settings.
    tolerances_used = Column(JSON, nullable=True)

    results = relationship("ReconciliationResult", back_populates="run",
                           cascade="all, delete-orphan")


class ReconciliationResult(Base):
    __tablename__ = "reconciliation_results"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    run_id = Column(String, ForeignKey("reconciliation_runs.id"), nullable=False, index=True)

    purchase_entry_id = Column(String, ForeignKey("purchase_register_entries.id"), nullable=True)
    gstr2b_entry_id = Column(String, ForeignKey("gstr2b_entries.id"), nullable=True)

    match_status = Column(_enum(MatchStatus, "match_status"), nullable=False, index=True)
    confidence_score = Column(Integer, nullable=False, default=0)   # 0-100
    plain_language_note = Column(Text, nullable=True)

    # Stable identity across re-runs: a hash of vendor GSTIN + normalised
    # invoice number + period. Lets an exception's human work follow the
    # same real-world invoice from one run to the next.
    match_key = Column(String(64), nullable=True, index=True)

    run = relationship("ReconciliationRun", back_populates="results")
    purchase_entry = relationship("PurchaseRegisterEntry")
    gstr2b_entry = relationship("GSTR2BEntry")
    exception = relationship("ReconciliationException", back_populates="result",
                             uselist=False, cascade="all, delete-orphan")


class ReconciliationException(Base, TimestampMixin):
    """Human workflow attached to a flagged result.

    Named `ReconciliationException` rather than `Exception` because the
    latter shadows the Python builtin. The table is still `exceptions`.
    """
    __tablename__ = "exceptions"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    reconciliation_result_id = Column(String, ForeignKey("reconciliation_results.id"),
                                      nullable=False, index=True)
    # Denormalised so the exception queue can be filtered without joining
    # up through run -> gstin.
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=False, index=True)
    period = Column(String(7), nullable=False, index=True)
    match_key = Column(String(64), nullable=True, index=True)

    status = Column(_enum(ExceptionStatus, "exception_status"),
                    nullable=False, default=ExceptionStatus.OPEN, index=True)
    assigned_to = Column(String, ForeignKey("users.id"), nullable=True, index=True)

    resolution_notes = Column(Text, nullable=True)
    resolved_by = Column(String, ForeignKey("users.id"), nullable=True)
    resolved_at = Column(DateTime, nullable=True)

    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rework_reason = Column(Text, nullable=True)

    # Statutory deadline after which the ITC for this invoice lapses.
    itc_lapse_deadline = Column(Date, nullable=True, index=True)

    result = relationship("ReconciliationResult", back_populates="exception")
    comments = relationship("ExceptionComment", back_populates="exception",
                            cascade="all, delete-orphan")


class ExceptionComment(Base):
    __tablename__ = "exception_comments"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    exception_id = Column(String, ForeignKey("exceptions.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    comment = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    exception = relationship("ReconciliationException", back_populates="comments")


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------

class Notice(Base, TimestampMixin, SoftDeleteMixin):
    """A GST notice received by a client, taken through to a filed response.

    Pipeline: RECEIVED -> UNDER_REVIEW -> RESPONSE_DRAFTED -> PARTNER_APPROVED
    -> FILED -> CLOSED, with a send-back from RESPONSE_DRAFTED to UNDER_REVIEW.
    The person who drafted the response can never be the person who approves
    it (see app/services/notices.py).

    The descriptive columns (type, dates, amount...) are the *working* values a
    person has reviewed. What the analyser originally read is kept untouched in
    `ai_analysis`, so a correction is visible as a difference between the two.
    """
    __tablename__ = "notices"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=True, index=True)

    title = Column(String(300), nullable=True)
    notice_type = Column(String(100), nullable=True)   # e.g. "SCRUTINY_ASMT10"
    notice_reference = Column(String(100), nullable=True)
    section = Column(String(100), nullable=True)       # e.g. "Section 61"
    notice_date = Column(Date, nullable=True)
    response_deadline = Column(Date, nullable=True, index=True)
    # False while the deadline is only what the analyser read from the text;
    # a person must confirm it against the original before a response can be
    # submitted, because a wrong deadline is the costliest error in this module.
    deadline_confirmed = Column(Boolean, nullable=False, default=False)
    period = Column(String(7), nullable=True)          # first month the notice covers
    period_to = Column(String(7), nullable=True)       # last month, when it spans several
    amount_in_question = Column(Numeric(14, 2), nullable=True)

    # The original file, encrypted, behind app/storage. The key is opaque.
    uploaded_file_path = Column(String(1000), nullable=True)
    original_filename = Column(String(400), nullable=True)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    file_sha256 = Column(String(64), nullable=True)

    ocr_text = Column(Text, nullable=True)
    extraction_status = Column(_enum(NoticeExtractionStatus, "notice_extraction_status"),
                               nullable=False, default=NoticeExtractionStatus.PENDING)
    extraction_method = Column(String(20), nullable=True)      # PDF_TEXT | OCR | MANUAL
    extraction_message = Column(Text, nullable=True)
    page_count = Column(Integer, nullable=True)

    ai_analysis = Column(JSON, nullable=True)
    analysis_status = Column(_enum(NoticeAnalysisStatus, "notice_analysis_status"),
                             nullable=False, default=NoticeAnalysisStatus.NOT_RUN)
    analysis_error = Column(Text, nullable=True)
    analysed_at = Column(DateTime, nullable=True)

    drafted_response = Column(Text, nullable=True)
    drafted_by = Column(String, ForeignKey("users.id"), nullable=True)      # last to change the text
    drafted_at = Column(DateTime, nullable=True)
    submitted_by = Column(String, ForeignKey("users.id"), nullable=True)    # sent it for approval
    submitted_at = Column(DateTime, nullable=True)
    rework_reason = Column(Text, nullable=True)

    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)

    filed_by = Column(String, ForeignKey("users.id"), nullable=True)
    filed_at = Column(Date, nullable=True)                 # the date it was filed on the portal
    filing_reference = Column(String(100), nullable=True)  # acknowledgement / ARN

    status = Column(_enum(NoticeStatus, "notice_status"),
                    nullable=False, default=NoticeStatus.RECEIVED, index=True)
    assigned_to = Column(String, ForeignKey("users.id"), nullable=True)
    outcome = Column(Text, nullable=True)
    closed_by = Column(String, ForeignKey("users.id"), nullable=True)
    closed_at = Column(DateTime, nullable=True)

    uploaded_by = Column(String, ForeignKey("users.id"), nullable=True)


# ---------------------------------------------------------------------------
# Document vault
# ---------------------------------------------------------------------------

class ClientDocument(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "client_documents"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=False, index=True)

    folder = Column(String(200), nullable=False, default="General")
    title = Column(String(300), nullable=False)
    original_filename = Column(String(400), nullable=True)
    file_path = Column(String(1000), nullable=False)
    content_type = Column(String(120), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    uploaded_by = Column(String, ForeignKey("users.id"), nullable=True)


# ---------------------------------------------------------------------------
# Audit, consent, billing, notifications
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """Append-only. Never updated, never deleted.

    The application layer simply never issues UPDATE or DELETE against this
    table. `prev_hash`/`entry_hash` chain each row to its predecessor so
    tampering is detectable rather than merely discouraged. On PostgreSQL a
    migration additionally installs a trigger that raises on UPDATE/DELETE;
    SQLite local development relies on the hash chain alone.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_firm_created", "firm_id", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)

    action = Column(String(80), nullable=False, index=True)   # e.g. "UPLOAD_PURCHASE_REGISTER"
    entity_type = Column(String(80), nullable=True)
    entity_id = Column(String, nullable=True)
    # `metadata` is reserved on the SQLAlchemy declarative base, so the
    # attribute is `meta` while the column keeps the specified name.
    meta = Column("metadata", JSON, nullable=True)

    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(400), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)

    # Tamper-evident hash chain
    prev_hash = Column(String(64), nullable=True)
    entry_hash = Column(String(64), nullable=True)


class ConsentLog(Base):
    """DPDP Act 2023: what was consented to, by whom, when, for what purpose."""
    __tablename__ = "consent_logs"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_id = Column(String, ForeignKey("clients.id"), nullable=True, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=True, index=True)

    consent_type = Column(_enum(ConsentType, "consent_type"), nullable=False)
    purpose = Column(Text, nullable=False)
    granted_at = Column(DateTime, nullable=False, default=utcnow)
    expires_at = Column(DateTime, nullable=True)
    withdrawn_at = Column(DateTime, nullable=True)
    granted_via = Column(String(50), nullable=True)   # "GSP_OTP", "ENGAGEMENT_LETTER"
    recorded_by = Column(String, ForeignKey("users.id"), nullable=True)
    evidence_reference = Column(String(300), nullable=True)


class GSPConsentAttempt(Base):
    """One attempt to obtain consent: an OTP request and what became of it.

    Exists for three reasons. It bounds OTP guessing (a limited number of
    verify attempts per request). It throttles OTP requests, which protects the
    GST portal and the client's phone from being spammed. And it is the record
    of who tried to connect which GSTIN, when, and how it ended.

    The OTP itself is never stored: it goes from the taxpayer's phone to the
    person typing it, straight through to the provider.
    """
    __tablename__ = "gsp_consent_attempts"
    __table_args__ = (
        Index("ix_gsp_attempt_gstin_requested", "client_gstin_id", "requested_at"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=False, index=True)

    provider = Column(String(50), nullable=False)
    username = Column(String(100), nullable=False)
    status = Column(_enum(ConsentAttemptStatus, "consent_attempt_status"),
                    nullable=False, default=ConsentAttemptStatus.PENDING)
    verify_attempts = Column(Integer, nullable=False, default=0)

    requested_by = Column(String, ForeignKey("users.id"), nullable=True)
    requested_at = Column(DateTime, nullable=False, default=utcnow)
    expires_at = Column(DateTime, nullable=False)      # how long the OTP may be entered
    completed_at = Column(DateTime, nullable=True)
    failure_code = Column(String(60), nullable=True)
    provider_transaction_id = Column(String(100), nullable=True)   # for support tickets


class GSTR2BFetchAttempt(Base):
    """Every try at fetching a GSTR-2B, successful or not.

    The scheduled job must never fail silently, and "the logs" is not an
    answer a CA can act on. This is the durable, queryable record: what was
    attempted, why it failed in words a person can read, and whether another
    try is coming. It also feeds the per-GSTIN status shown in the app and,
    later, the operator's view of GSP error rates.
    """
    __tablename__ = "gstr2b_fetch_attempts"
    __table_args__ = (
        Index("ix_fetch_attempt_gstin_period", "client_gstin_id", "period", "created_at"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    client_gstin_id = Column(String, ForeignKey("client_gstins.id"), nullable=False, index=True)
    period = Column(String(7), nullable=False)

    trigger = Column(_enum(FetchTrigger, "fetch_trigger"), nullable=False)
    status = Column(_enum(FetchAttemptStatus, "fetch_attempt_status"), nullable=False, index=True)
    attempt_number = Column(Integer, nullable=False, default=1)
    error_code = Column(String(60), nullable=True)
    message = Column(Text, nullable=True)              # written for a person
    provider_transaction_id = Column(String(100), nullable=True)
    next_retry_at = Column(DateTime, nullable=True)

    fetch_id = Column(String, ForeignKey("gstr2b_fetches.id"), nullable=True)
    reconciliation_run_id = Column(String, ForeignKey("reconciliation_runs.id"), nullable=True)
    invoice_count = Column(Integer, nullable=True)
    credit_note_count = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    tier = Column(_enum(SubscriptionTier, "sub_tier"),
                  nullable=False, default=SubscriptionTier.STARTER)
    status = Column(_enum(SubscriptionStatus, "sub_status"),
                    nullable=False, default=SubscriptionStatus.TRIALING)
    razorpay_subscription_id = Column(String(100), nullable=True, index=True)
    razorpay_customer_id = Column(String(100), nullable=True)
    current_period_start = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True, index=True)
    cancelled_at = Column(DateTime, nullable=True)


class UsageRecord(Base):
    """Per-firm, per-month usage counters driving plan limits and billing."""
    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint("firm_id", "period", name="uq_usage_firm_period"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    period = Column(String(7), nullable=False, index=True)

    active_clients = Column(Integer, nullable=False, default=0)
    active_gstins = Column(Integer, nullable=False, default=0)
    reconciliations_run = Column(Integer, nullable=False, default=0)
    notices_processed = Column(Integer, nullable=False, default=0)
    storage_bytes = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notification_user_read", "user_id", "read_status"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    type = Column(String(60), nullable=False, index=True)
    title = Column(String(300), nullable=False)
    message = Column(Text, nullable=True)
    link = Column(String(500), nullable=True)
    read_status = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utcnow, index=True)

    # Set when an email was dispatched for this notification, so a retry or a
    # re-run of a scheduled job cannot email the same person twice.
    emailed_at = Column(DateTime, nullable=True)

    # Groups notifications about the same underlying thing, so a digest can
    # collapse them and a duplicate can be suppressed.
    entity_type = Column(String(80), nullable=True)
    entity_id = Column(String, nullable=True, index=True)


class NotificationPreference(Base):
    """Per-user, per-event delivery choice.

    A row exists only where the user has changed something. Absence means
    "use the default for this event type", which keeps the table small and
    means adding a new event type does not require backfilling every user.
    """
    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", "event_type", name="uq_notif_pref_user_event"),
    )

    id = Column(String, primary_key=True, default=gen_id)
    firm_id = Column(String, ForeignKey("firms.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    event_type = Column(String(60), nullable=False)
    in_app = Column(Boolean, nullable=False, default=True)
    email = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


# Every model carrying a firm_id, i.e. every table that must always be
# tenant-filtered. app/tenancy.py uses this list to verify that a scoped
# query was actually applied, so adding a model here is a deliberate act.
TENANT_SCOPED_MODELS = (
    User, RefreshToken, Invitation, Client, ClientGSTIN,
    AuditEngagement, FirmAuditPolicy, MaterialityAssessment,
    CAROApplicabilityAssessment, CAROClauseResponse, AuditAIRecord, ManagementRepLetter,
    WorkingPaper, AuditProcedure, TrialBalanceUpload, TrialBalanceLine, RiskFlag,
    StatutoryFilingRecord, LedgerUpload, LedgerLine, AgeingSchedule, RelatedParty,
    ClientBankAccount, BankConfirmation, AuditRatioAnalysis,
    PurchaseRegisterUpload, PurchaseRegisterEntry,
    GSTR1Return, GSTR1SalesUpload, GSTR1ClientFormatProfile, GSTR1SalesInvoice, GSTR1ValidationIssue, GSTR1DocumentIssued, GSTR1TenantRateReference,
    GSTR2BFetch, GSTR2BEntry,
    ReconciliationRun, ReconciliationResult, ReconciliationException,
    ExceptionComment, Notice, ClientDocument, AuditLog, ConsentLog,
    Subscription, UsageRecord, Notification, NotificationPreference,
    GSPConsentAttempt, GSTR2BFetchAttempt,
)
