"""Encrypted client bank master and human-approved bank confirmation workflow."""
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError

from app import audit, crypto, models, schemas
from app.audit import AuditAction
from app.deps import Role, client_ip, get_tenant, require_role, user_agent
from app.tenancy import TenantSession

router = APIRouter(prefix="/api", tags=["audit-bank-confirmations"])


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mask(last4: str) -> str:
    return f"•••• {last4}"


def _account_out(account):
    return schemas.ClientBankAccountOut(
        id=account.id, client_id=account.client_id, bank_name=account.bank_name,
        branch_name=account.branch_name, account_name=account.account_name,
        masked_account_number=_mask(account.account_number_last4), ifsc_code=account.ifsc_code,
        is_active=account.is_active, created_by=account.created_by,
        created_at=account.created_at, updated_at=account.updated_at,
    )


def _engagement(tenant: TenantSession, engagement_id: str):
    return tenant.get_or_404(models.AuditEngagement, engagement_id, "Audit engagement not found")


def _confirmation(tenant, engagement, confirmation_id):
    row = tenant.get(models.BankConfirmation, confirmation_id)
    if row is None or row.engagement_id != engagement.id:
        raise HTTPException(404, "Bank confirmation not found")
    return row


@router.get("/clients/{client_id}/bank-accounts", response_model=list[schemas.ClientBankAccountOut])
def list_client_bank_accounts(client_id: str, include_inactive: bool = False,
                              _: models.User = Depends(require_role(Role.ASSOCIATE)),
                              tenant: TenantSession = Depends(get_tenant)):
    tenant.get_or_404(models.Client, client_id, "Client not found")
    query = tenant.query(models.ClientBankAccount).filter(models.ClientBankAccount.client_id == client_id)
    if not include_inactive:
        query = query.filter(models.ClientBankAccount.is_active.is_(True))
    return [_account_out(row) for row in query.order_by(models.ClientBankAccount.bank_name.asc()).all()]


@router.post("/clients/{client_id}/bank-accounts", response_model=schemas.ClientBankAccountOut,
             status_code=status.HTTP_201_CREATED)
def create_client_bank_account(client_id: str, payload: schemas.ClientBankAccountCreate,
                               request: Request,
                               actor: models.User = Depends(require_role(Role.MANAGER)),
                               tenant: TenantSession = Depends(get_tenant)):
    tenant.get_or_404(models.Client, client_id, "Client not found")
    number = payload.account_number.strip().upper()
    row = models.ClientBankAccount(
        client_id=client_id, bank_name=payload.bank_name.strip(),
        branch_name=payload.branch_name, account_name=payload.account_name,
        account_number_encrypted=crypto.encrypt(number),
        account_number_last4=number[-4:],
        account_number_blind_index=crypto.blind_index(number),
        ifsc_code=payload.ifsc_code.upper() if payload.ifsc_code else None,
        created_by=actor.id,
    )
    tenant.add(row)
    tenant.flush()
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.CLIENT_BANK_ACCOUNT_CREATED, entity_type="ClientBankAccount",
        entity_id=row.id, metadata={"client_id": client_id, "bank_name": row.bank_name,
        "masked_account_number": _mask(row.account_number_last4), "ifsc_code": row.ifsc_code},
        ip_address=client_ip(request), user_agent=user_agent(request))
    try:
        tenant.commit()
    except IntegrityError as exc:
        tenant.rollback()
        raise HTTPException(409, "This bank account is already on file for the client.") from exc
    tenant.refresh(row)
    return _account_out(row)


@router.patch("/clients/{client_id}/bank-accounts/{account_id}", response_model=schemas.ClientBankAccountOut)
def update_client_bank_account(client_id: str, account_id: str,
                               payload: schemas.ClientBankAccountUpdate, request: Request,
                               actor: models.User = Depends(require_role(Role.MANAGER)),
                               tenant: TenantSession = Depends(get_tenant)):
    tenant.get_or_404(models.Client, client_id, "Client not found")
    account = tenant.get(models.ClientBankAccount, account_id)
    if account is None or account.client_id != client_id:
        raise HTTPException(404, "Client bank account not found")
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "Supply at least one bank-account field to update.")
    for key, value in changes.items():
        if key == "ifsc_code" and value:
            value = value.upper()
        setattr(account, key, value)
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.CLIENT_BANK_ACCOUNT_UPDATED, entity_type="ClientBankAccount",
        entity_id=account.id, metadata={"client_id": client_id, "changed_fields": sorted(changes)},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(account)
    return _account_out(account)


@router.delete("/clients/{client_id}/bank-accounts/{account_id}", response_model=schemas.ClientBankAccountOut)
def deactivate_client_bank_account(client_id: str, account_id: str, request: Request,
                                   actor: models.User = Depends(require_role(Role.MANAGER)),
                                   tenant: TenantSession = Depends(get_tenant)):
    tenant.get_or_404(models.Client, client_id, "Client not found")
    account = tenant.get(models.ClientBankAccount, account_id)
    if account is None or account.client_id != client_id:
        raise HTTPException(404, "Client bank account not found")
    account.is_active = False
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.CLIENT_BANK_ACCOUNT_DEACTIVATED, entity_type="ClientBankAccount",
        entity_id=account.id, metadata={"client_id": client_id},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(account)
    return _account_out(account)


@router.post("/audit-engagements/{engagement_id}/bank-confirmations/generate",
             response_model=list[schemas.BankConfirmationOut])
def generate_bank_confirmation_drafts(engagement_id: str, payload: schemas.BankConfirmationGenerateIn,
                                      request: Request,
                                      actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                                      tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    if engagement.status not in (models.AuditEngagementStatus.PLANNING,
                                  models.AuditEngagementStatus.FIELDWORK,
                                  models.AuditEngagementStatus.COMPLETION):
        raise HTTPException(409, "Bank-confirmation drafts cannot be generated after Reporting begins.")
    fy_start = date(int(engagement.financial_year[:4]), 4, 1)
    fy_end = date(fy_start.year + 1, 3, 31)
    if not fy_start <= payload.as_of_date <= fy_end:
        raise HTTPException(422, "Confirmation as_of_date must fall within the engagement financial year.")
    accounts = tenant.query(models.ClientBankAccount).filter(
        models.ClientBankAccount.client_id == engagement.client_id,
        models.ClientBankAccount.is_active.is_(True),
    ).all()
    if not accounts:
        raise HTTPException(409, "No active client bank accounts are on file.")
    output = []
    for account in accounts:
        prior = tenant.query(models.BankConfirmation).filter(
            models.BankConfirmation.engagement_id == engagement.id,
            models.BankConfirmation.bank_account_id == account.id,
            models.BankConfirmation.as_of_date == payload.as_of_date,
        ).order_by(models.BankConfirmation.version.desc()).first()
        version = prior.version + 1 if prior else 1
        if prior and prior.letter_status != models.BankConfirmationStatus.DRAFTED:
            raise HTTPException(409, "A confirmation for this account and date has already entered the approval/send workflow.")
        letter = (
            "[FIRM LETTERHEAD PLACEHOLDER]\n"
            f"Date: [Insert date]\n\nTo: The Branch Manager, {account.bank_name}"
            f"{', ' + account.branch_name if account.branch_name else ''}\n\n"
            "Subject: Request for direct bank balance confirmation\n\n"
            f"Please confirm directly to [AUDIT FIRM NAME / ADDRESS] the balance and other terms "
            f"of the account held by {engagement.client.name}, account ending {_mask(account.account_number_last4)}, "
            f"as at {payload.as_of_date.isoformat()}. Please include relevant facilities, guarantees, "
            "charges, liens, and other information requested in the attached confirmation schedule.\n\n"
            "This document is a draft for CA review. It has not been sent and makes no audit conclusion.\n\n"
            "For [AUDIT FIRM NAME]\n[Authorized signatory placeholder]"
        )
        row = models.BankConfirmation(
            engagement_id=engagement.id, bank_account_id=account.id,
            account_number_last4=account.account_number_last4,
            version=version, as_of_date=payload.as_of_date,
            letter_status=models.BankConfirmationStatus.DRAFTED,
            draft_content=letter, drafted_by=actor.id,
        )
        tenant.add(row)
        tenant.flush()
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.BANK_CONFIRMATION_DRAFTED, entity_type="BankConfirmation",
            entity_id=row.id, metadata={"engagement_id": engagement.id,
            "bank_account_id": account.id, "masked_account_number": _mask(account.account_number_last4),
            "as_of_date": row.as_of_date.isoformat(), "version": version,
            "status": row.letter_status.value, "draft_content": row.draft_content},
            ip_address=client_ip(request), user_agent=user_agent(request))
        output.append(row)
    tenant.commit()
    for row in output:
        tenant.refresh(row)
    return output


@router.get("/audit-engagements/{engagement_id}/bank-confirmations",
            response_model=list[schemas.BankConfirmationOut])
def list_bank_confirmations(engagement_id: str,
                            actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                            tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    return tenant.query(models.BankConfirmation).filter(
        models.BankConfirmation.engagement_id == engagement.id,
    ).order_by(models.BankConfirmation.as_of_date.desc(), models.BankConfirmation.version.desc()).all()


@router.post("/audit-engagements/{engagement_id}/bank-confirmations/{confirmation_id}/approve",
             response_model=schemas.BankConfirmationOut)
def approve_bank_confirmation(engagement_id: str, confirmation_id: str, request: Request,
                              actor: models.User = Depends(require_role(Role.MANAGER)),
                              tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    row = _confirmation(tenant, engagement, confirmation_id)
    if row.letter_status != models.BankConfirmationStatus.DRAFTED or row.approved_by:
        raise HTTPException(409, "Only an unapproved draft can be approved.")
    if row.drafted_by == actor.id:
        raise HTTPException(409, "A preparer cannot approve their own bank-confirmation draft.")
    row.approved_by, row.approved_at = actor.id, _now()
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.BANK_CONFIRMATION_APPROVED, entity_type="BankConfirmation",
        entity_id=row.id, metadata={"engagement_id": engagement.id, "drafted_by": row.drafted_by,
        "approved_by": actor.id, "version": row.version},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row


@router.post("/audit-engagements/{engagement_id}/bank-confirmations/{confirmation_id}/send",
             response_model=schemas.BankConfirmationOut)
def mark_bank_confirmation_sent(engagement_id: str, confirmation_id: str, request: Request,
                                actor: models.User = Depends(require_role(Role.MANAGER)),
                                tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    row = _confirmation(tenant, engagement, confirmation_id)
    if row.letter_status != models.BankConfirmationStatus.DRAFTED or not row.approved_by:
        raise HTTPException(409, "An approved draft is required before it can be marked sent.")
    row.letter_status, row.sent_by, row.sent_at = models.BankConfirmationStatus.SENT, actor.id, _now()
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.BANK_CONFIRMATION_MARKED_SENT, entity_type="BankConfirmation",
        entity_id=row.id, metadata={"engagement_id": engagement.id, "approved_by": row.approved_by,
        "sent_by": actor.id, "sent_at": row.sent_at.isoformat()},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row


@router.post("/audit-engagements/{engagement_id}/bank-confirmations/{confirmation_id}/pending",
             response_model=schemas.BankConfirmationOut)
def mark_bank_confirmation_pending(engagement_id: str, confirmation_id: str, request: Request,
                                   actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                                   tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    row = _confirmation(tenant, engagement, confirmation_id)
    if row.letter_status != models.BankConfirmationStatus.SENT:
        raise HTTPException(409, "Only a sent confirmation can be marked pending.")
    row.letter_status = models.BankConfirmationStatus.PENDING
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.BANK_CONFIRMATION_MARKED_PENDING, entity_type="BankConfirmation",
        entity_id=row.id, metadata={"engagement_id": engagement.id, "status": row.letter_status.value},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row


@router.post("/audit-engagements/{engagement_id}/bank-confirmations/{confirmation_id}/receive",
             response_model=schemas.BankConfirmationOut)
def receive_bank_confirmation(engagement_id: str, confirmation_id: str,
                              payload: schemas.BankConfirmationReceiveIn, request: Request,
                              actor: models.User = Depends(require_role(Role.ASSOCIATE)),
                              tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    row = _confirmation(tenant, engagement, confirmation_id)
    if row.letter_status not in (models.BankConfirmationStatus.SENT, models.BankConfirmationStatus.PENDING):
        raise HTTPException(409, "A sent or pending confirmation is required before recording receipt.")
    row.books_balance = payload.books_balance
    row.confirmed_balance = payload.confirmed_balance
    row.discrepancy_amount = (payload.confirmed_balance - payload.books_balance).quantize(Decimal("0.01"))
    row.discrepancy_notes = payload.discrepancy_notes
    row.received_at = _now()
    row.letter_status = models.BankConfirmationStatus.RECEIVED
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.BANK_CONFIRMATION_RECEIVED, entity_type="BankConfirmation",
        entity_id=row.id, metadata={"engagement_id": engagement.id, "books_balance": str(row.books_balance),
        "confirmed_balance": str(row.confirmed_balance), "discrepancy_amount": str(row.discrepancy_amount)},
        ip_address=client_ip(request), user_agent=user_agent(request))
    if row.discrepancy_amount != 0:
        note = (f"SYSTEM FLAG — bank confirmation for account ending {_mask(row.account_number_last4)} "
                f"differs from the books by ₹{abs(row.discrepancy_amount):,.2f}. "
                "Investigate and document the reconciling items; no audit conclusion is inferred.")
        flag = models.RiskFlag(
            engagement_id=engagement.id, policy_version=None,
            flag_type=models.RiskFlagType.OTHER, severity="REVIEW_REQUIRED",
            system_note=note, status=models.RiskFlagStatus.SYSTEM_FLAGGED,
            trigger_data={"subtype": "BANK_CONFIRMATION_DISCREPANCY", "confirmation_id": row.id,
                          "books_balance": str(row.books_balance), "confirmed_balance": str(row.confirmed_balance),
                          "discrepancy_amount": str(row.discrepancy_amount)},
        )
        tenant.add(flag)
        tenant.flush()
        audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
            action=AuditAction.AUDIT_RISK_FLAG_SYSTEM_GENERATED, entity_type="RiskFlag",
            entity_id=flag.id, metadata={"engagement_id": engagement.id,
            "bank_confirmation_id": row.id, "status": "SYSTEM_FLAGGED", "system_note": note},
            ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row


@router.post("/audit-engagements/{engagement_id}/bank-confirmations/{confirmation_id}/reconcile",
             response_model=schemas.BankConfirmationOut)
def reconcile_bank_confirmation(engagement_id: str, confirmation_id: str,
                                payload: schemas.BankConfirmationReconcileIn, request: Request,
                                actor: models.User = Depends(require_role(Role.MANAGER)),
                                tenant: TenantSession = Depends(get_tenant)):
    engagement = _engagement(tenant, engagement_id)
    row = _confirmation(tenant, engagement, confirmation_id)
    if row.letter_status != models.BankConfirmationStatus.RECEIVED:
        raise HTTPException(409, "A received bank confirmation must be reviewed before reconciliation.")
    if row.drafted_by == actor.id:
        raise HTTPException(409, "The preparer cannot approve reconciliation of their own confirmation.")
    if row.discrepancy_amount and not payload.notes:
        raise HTTPException(422, "Document the treatment of the identified difference before reconciliation.")
    if payload.notes:
        row.discrepancy_notes = (row.discrepancy_notes + "\n" if row.discrepancy_notes else "") + payload.notes
    row.letter_status, row.reconciled_by, row.reconciled_at = models.BankConfirmationStatus.RECONCILED, actor.id, _now()
    audit.record(tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.BANK_CONFIRMATION_RECONCILED, entity_type="BankConfirmation",
        entity_id=row.id, metadata={"engagement_id": engagement.id, "reconciled_by": actor.id,
        "discrepancy_amount": str(row.discrepancy_amount), "notes": payload.notes},
        ip_address=client_ip(request), user_agent=user_agent(request))
    tenant.commit()
    tenant.refresh(row)
    return row
