"""
Append-only, tamper-evident audit logging.

Every data-modifying action records who did what, to which entity, when.
Two properties make the log worth trusting during an audit:

**Append-only.** The application never issues UPDATE or DELETE against
`audit_logs`. On PostgreSQL a migration additionally installs a trigger
that raises on either statement, so the guarantee does not depend on
application discipline alone.

**Tamper-evident.** Each row stores `entry_hash`, a SHA-256 over the row's
own fields plus the previous row's hash. Altering or removing any historical
row breaks every hash after it, and `verify_chain()` reports exactly where.
A plain log can be edited by anyone with database access and leave no trace;
a chained one cannot be edited without detection.

Note this makes tampering *detectable*, not *impossible*. Someone with write
access could recompute the whole chain. Defending against that requires
periodically anchoring the head hash somewhere outside the database, which
is worth doing before an external audit but is out of scope here.
"""
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app import models

logger = logging.getLogger("gstr2b.audit")

GENESIS_HASH = "0" * 64


# --- Action vocabulary -----------------------------------------------------
# Kept as constants so queries can filter reliably and typos surface early.

class AuditAction:
    # Authentication
    FIRM_SIGNUP = "FIRM_SIGNUP"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    LOGOUT_ALL_DEVICES = "LOGOUT_ALL_DEVICES"
    TOKEN_REFRESHED = "TOKEN_REFRESHED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"
    PASSWORD_RESET_COMPLETED = "PASSWORD_RESET_COMPLETED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    TOTP_ENABLED = "TOTP_ENABLED"
    TOTP_DISABLED = "TOTP_DISABLED"

    # Staff
    USER_INVITED = "USER_INVITED"
    INVITATION_ACCEPTED = "INVITATION_ACCEPTED"
    INVITATION_REVOKED = "INVITATION_REVOKED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_DEACTIVATED = "USER_DEACTIVATED"

    # Clients
    CLIENT_CREATED = "CLIENT_CREATED"
    CLIENT_UPDATED = "CLIENT_UPDATED"
    CLIENT_DELETED = "CLIENT_DELETED"
    RELATED_PARTY_CREATED = "RELATED_PARTY_CREATED"
    RELATED_PARTY_UPDATED = "RELATED_PARTY_UPDATED"
    RELATED_PARTY_DEACTIVATED = "RELATED_PARTY_DEACTIVATED"
    AUDIT_ENGAGEMENT_CREATED = "AUDIT_ENGAGEMENT_CREATED"
    AUDIT_POLICY_DRAFTED = "AUDIT_POLICY_DRAFTED"
    AUDIT_POLICY_APPROVED = "AUDIT_POLICY_APPROVED"
    TRIAL_BALANCE_UPLOADED = "TRIAL_BALANCE_UPLOADED"
    AUDIT_RISK_FLAG_SYSTEM_GENERATED = "AUDIT_RISK_FLAG_SYSTEM_GENERATED"
    STATUTORY_FILING_RECORDED = "STATUTORY_FILING_RECORDED"
    STATUTORY_FILING_REVIEWED = "STATUTORY_FILING_REVIEWED"
    AUDIT_LEDGER_UPLOADED = "AUDIT_LEDGER_UPLOADED"
    AUDIT_AGEING_SCHEDULE_GENERATED = "AUDIT_AGEING_SCHEDULE_GENERATED"
    CLIENT_BANK_ACCOUNT_CREATED = "CLIENT_BANK_ACCOUNT_CREATED"
    CLIENT_BANK_ACCOUNT_UPDATED = "CLIENT_BANK_ACCOUNT_UPDATED"
    CLIENT_BANK_ACCOUNT_DEACTIVATED = "CLIENT_BANK_ACCOUNT_DEACTIVATED"
    BANK_CONFIRMATION_DRAFTED = "BANK_CONFIRMATION_DRAFTED"
    BANK_CONFIRMATION_APPROVED = "BANK_CONFIRMATION_APPROVED"
    BANK_CONFIRMATION_MARKED_SENT = "BANK_CONFIRMATION_MARKED_SENT"
    BANK_CONFIRMATION_MARKED_PENDING = "BANK_CONFIRMATION_MARKED_PENDING"
    BANK_CONFIRMATION_RECEIVED = "BANK_CONFIRMATION_RECEIVED"
    BANK_CONFIRMATION_RECONCILED = "BANK_CONFIRMATION_RECONCILED"
    AUDIT_RATIO_ANALYSIS_GENERATED = "AUDIT_RATIO_ANALYSIS_GENERATED"
    AUDIT_RATIO_ANALYSIS_REVIEWED = "AUDIT_RATIO_ANALYSIS_REVIEWED"
    AUDIT_RATIO_ANALYSIS_APPROVED = "AUDIT_RATIO_ANALYSIS_APPROVED"
    MATERIALITY_SUGGESTED = "MATERIALITY_SUGGESTED"
    MATERIALITY_CONFIRMED = "MATERIALITY_CONFIRMED"
    CARO_APPLICABILITY_ASSESSED = "CARO_APPLICABILITY_ASSESSED"
    CARO_APPLICABILITY_CONFIRMED = "CARO_APPLICABILITY_CONFIRMED"
    CARO_RESPONSE_DRAFT_GENERATED = "CARO_RESPONSE_DRAFT_GENERATED"
    CARO_RESPONSE_EDITED = "CARO_RESPONSE_EDITED"
    CARO_RESPONSE_MANAGER_REVIEWED = "CARO_RESPONSE_MANAGER_REVIEWED"
    CARO_RESPONSE_FINALIZED = "CARO_RESPONSE_FINALIZED"
    AUDIT_AI_SUGGESTION_RECORDED = "AUDIT_AI_SUGGESTION_RECORDED"
    MANAGEMENT_REP_LETTER_DRAFTED = "MANAGEMENT_REP_LETTER_DRAFTED"
    MANAGEMENT_REP_LETTER_EDITED = "MANAGEMENT_REP_LETTER_EDITED"
    MANAGEMENT_REP_LETTER_REVIEWED = "MANAGEMENT_REP_LETTER_REVIEWED"
    MANAGEMENT_REP_LETTER_APPROVED = "MANAGEMENT_REP_LETTER_APPROVED"
    MANAGEMENT_REP_LETTER_SENT = "MANAGEMENT_REP_LETTER_SENT"
    MANAGEMENT_REP_LETTER_RECEIVED = "MANAGEMENT_REP_LETTER_RECEIVED"
    WORKING_PAPER_COMPILED = "WORKING_PAPER_COMPILED"
    WORKING_PAPER_REVIEWED = "WORKING_PAPER_REVIEWED"
    WORKING_PAPER_APPROVED = "WORKING_PAPER_APPROVED"
    AUDIT_PACKAGE_EXPORTED = "AUDIT_PACKAGE_EXPORTED"
    AUDIT_ENGAGEMENT_CLOSED = "AUDIT_ENGAGEMENT_CLOSED"
    AUDIT_ENGAGEMENT_MOVED_TO_REPORTING = "AUDIT_ENGAGEMENT_MOVED_TO_REPORTING"
    AUDIT_ENGAGEMENT_MOVED_TO_COMPLETION = "AUDIT_ENGAGEMENT_MOVED_TO_COMPLETION"
    AUDIT_PROCEDURE_PLANNED = "AUDIT_PROCEDURE_PLANNED"
    AUDIT_PROCEDURE_PERFORMED = "AUDIT_PROCEDURE_PERFORMED"
    AUDIT_PROCEDURE_REVIEWED = "AUDIT_PROCEDURE_REVIEWED"
    AUDIT_PROCEDURE_APPROVED = "AUDIT_PROCEDURE_APPROVED"
    AUDIT_FIELDWORK_STARTED = "AUDIT_FIELDWORK_STARTED"
    GSTIN_ADDED = "GSTIN_ADDED"
    GSTIN_REMOVED = "GSTIN_REMOVED"

    # Data
    UPLOAD_PURCHASE_REGISTER = "UPLOAD_PURCHASE_REGISTER"
    GSTR1_SALES_REGISTER_UPLOADED = "GSTR1_SALES_REGISTER_UPLOADED"
    GSTR1_INVOICE_REVIEWED = "GSTR1_INVOICE_REVIEWED"
    GSTR1_SUBMITTED_FOR_PARTNER_REVIEW = "GSTR1_SUBMITTED_FOR_PARTNER_REVIEW"
    GSTR1_RETURN_PARTNER_APPROVED = "GSTR1_RETURN_PARTNER_APPROVED"
    GSTR1_DRAFT_EXPORTED = "GSTR1_DRAFT_EXPORTED"
    GSTR1_APPROVED_EXPORT_GENERATED = "GSTR1_APPROVED_EXPORT_GENERATED"
    GSTR1_DOCUMENTS_ISSUED_RECORDED = "GSTR1_DOCUMENTS_ISSUED_RECORDED"
    GSTR1_DOCUMENTS_ISSUED_REVIEWED = "GSTR1_DOCUMENTS_ISSUED_REVIEWED"
    GSTR1_SOURCE_REGISTER_DOWNLOADED = "GSTR1_SOURCE_REGISTER_DOWNLOADED"
    GSTR1_BOOKS_RECONCILIATION_RECORDED = "GSTR1_BOOKS_RECONCILIATION_RECORDED"
    GSTR1_GST_RATE_REFERENCE_MAINTAINED = "GSTR1_GST_RATE_REFERENCE_MAINTAINED"
    FETCH_GSTR2B = "FETCH_GSTR2B"
    RECONCILIATION_RUN = "RECONCILIATION_RUN"

    # Exceptions
    EXCEPTION_ASSIGNED = "EXCEPTION_ASSIGNED"
    EXCEPTION_RESOLVED = "EXCEPTION_RESOLVED"
    EXCEPTION_APPROVED = "EXCEPTION_APPROVED"
    EXCEPTION_REWORK = "EXCEPTION_REWORK"
    EXCEPTION_COMMENTED = "EXCEPTION_COMMENTED"

    # Notices
    NOTICE_UPLOADED = "NOTICE_UPLOADED"
    NOTICE_UPDATED = "NOTICE_UPDATED"
    NOTICE_ANALYSED = "NOTICE_ANALYSED"
    NOTICE_TEXT_EDITED = "NOTICE_TEXT_EDITED"
    NOTICE_ASSIGNED = "NOTICE_ASSIGNED"
    NOTICE_DRAFT_GENERATED = "NOTICE_DRAFT_GENERATED"
    NOTICE_DRAFT_EDITED = "NOTICE_DRAFT_EDITED"
    NOTICE_STATUS_CHANGED = "NOTICE_STATUS_CHANGED"
    NOTICE_FILE_DOWNLOADED = "NOTICE_FILE_DOWNLOADED"
    NOTICE_DELETED = "NOTICE_DELETED"

    # Notices
    NOTICE_UPLOADED = "NOTICE_UPLOADED"
    NOTICE_UPDATED = "NOTICE_UPDATED"
    NOTICE_ANALYSED = "NOTICE_ANALYSED"
    NOTICE_TEXT_EDITED = "NOTICE_TEXT_EDITED"
    NOTICE_ASSIGNED = "NOTICE_ASSIGNED"
    NOTICE_DRAFT_GENERATED = "NOTICE_DRAFT_GENERATED"
    NOTICE_DRAFT_EDITED = "NOTICE_DRAFT_EDITED"
    NOTICE_STATUS_CHANGED = "NOTICE_STATUS_CHANGED"
    NOTICE_FILE_DOWNLOADED = "NOTICE_FILE_DOWNLOADED"
    NOTICE_DELETED = "NOTICE_DELETED"

    # Consent / GSP
    CONSENT_GRANTED = "CONSENT_GRANTED"
    CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
    GSP_CONSENT_REQUESTED = "GSP_CONSENT_REQUESTED"
    GSP_GSTIN_VERIFIED = "GSP_GSTIN_VERIFIED"

    # Platform
    ADMIN_IMPERSONATION = "ADMIN_IMPERSONATION"
    EXPORT_GENERATED = "EXPORT_GENERATED"


def _canonical(payload: dict) -> str:
    """Stable JSON so the same content always hashes identically."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _compute_entry_hash(
    *, firm_id: str, user_id: Optional[str], action: str,
    entity_type: Optional[str], entity_id: Optional[str],
    meta: Optional[dict], created_at: datetime, prev_hash: str,
) -> str:
    return hashlib.sha256(_canonical({
        "firm_id": firm_id,
        "user_id": user_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "metadata": meta,
        "created_at": created_at.isoformat(),
        "prev_hash": prev_hash,
    }).encode("utf-8")).hexdigest()


def _chain_head(db: Session, firm_id: str) -> tuple[str, Optional[datetime]]:
    """(hash, timestamp) of the last entry in this firm's chain.

    Each firm keeps its own chain, so one firm's activity volume cannot
    interfere with another's verification.

    Pending entries are flushed first. Sessions here run with autoflush off, so
    a second `record()` in the same request would otherwise not see the first,
    read the same head, and fork the chain: both entries claiming the same
    predecessor, which `verify_chain` then reports as tampering.
    """
    db.flush()
    row = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.firm_id == firm_id)
        .order_by(models.AuditLog.created_at.desc(), models.AuditLog.id.desc())
        .first()
    )
    if row is None or not row.entry_hash:
        return GENESIS_HASH, None
    return row.entry_hash, row.created_at


def _latest_hash(db: Session, firm_id: str) -> str:
    return _chain_head(db, firm_id)[0]


def record(
    db: Session,
    *,
    firm_id: str,
    action: str,
    user_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> models.AuditLog:
    """Append one audit entry. Caller commits.

    Takes a raw Session rather than a TenantSession because auditing must
    work during authentication, before a tenant scope exists — a failed
    login has no authenticated user but must still be recorded.
    """
    prev_hash, prev_created_at = _chain_head(db, firm_id)
    created_at = datetime.utcnow()
    if prev_created_at is not None and created_at <= prev_created_at:
        # Two entries in the same clock tick would tie on timestamp and then be
        # ordered by random id, which need not match the order they chain in.
        # Strictly increasing timestamps make the order unambiguous.
        created_at = prev_created_at + timedelta(microseconds=1)
    entry_hash = _compute_entry_hash(
        firm_id=firm_id, user_id=user_id, action=action,
        entity_type=entity_type, entity_id=entity_id, meta=metadata,
        created_at=created_at, prev_hash=prev_hash,
    )

    entry = models.AuditLog(
        firm_id=firm_id,
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        meta=metadata,
        ip_address=ip_address,
        user_agent=(user_agent or "")[:400] or None,
        created_at=created_at,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    db.add(entry)
    return entry


def verify_chain(db: Session, firm_id: str) -> dict:
    """Recompute the firm's hash chain and report the first break.

    Returns {"valid": bool, "checked": int, "broken_at": id|None, "reason": str|None}.
    """
    rows = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.firm_id == firm_id)
        .order_by(models.AuditLog.created_at.asc(), models.AuditLog.id.asc())
        .all()
    )

    expected_prev = GENESIS_HASH
    for i, row in enumerate(rows):
        if row.prev_hash != expected_prev:
            return {
                "valid": False, "checked": i, "broken_at": row.id,
                "reason": "prev_hash does not match the preceding entry — "
                          "an entry was likely inserted or removed.",
            }
        recomputed = _compute_entry_hash(
            firm_id=row.firm_id, user_id=row.user_id, action=row.action,
            entity_type=row.entity_type, entity_id=row.entity_id,
            meta=row.meta, created_at=row.created_at, prev_hash=row.prev_hash,
        )
        if recomputed != row.entry_hash:
            return {
                "valid": False, "checked": i, "broken_at": row.id,
                "reason": "entry_hash does not match the row contents — "
                          "this entry was modified after it was written.",
            }
        expected_prev = row.entry_hash

    return {"valid": True, "checked": len(rows), "broken_at": None, "reason": None}
