"""
Authentication, session management and staff onboarding.

Design notes worth knowing before changing anything here:

**Refresh tokens rotate.** Every refresh revokes the presented token and
issues a new one. If a token that was already revoked is presented again,
that means it was captured and replayed, so the entire family of tokens for
that user is revoked and they must log in again. A non-rotating refresh
token is a bearer credential valid for a month; this limits the damage.

**The refresh token lives in an httpOnly cookie**, never in the JSON body,
so that a cross-site scripting bug cannot read it. The short-lived access
token is returned in the body for the SPA to hold in memory.

**Login responses do not reveal whether an email exists.** Wrong password
and unknown account produce the same message and, as far as practical, the
same work, because a difference is an account-enumeration oracle.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app import audit, models, schemas
from app.audit import AuditAction
from app.config import settings
from app.crypto import decrypt, encrypt, generate_token, hash_token
from app.database import get_db
from app.deps import (
    Role, client_ip, get_current_user, get_tenant, require_role, user_agent,
)
from app.security import (
    PasswordPolicyError, create_access_token, hash_password, needs_rehash,
    validate_password_strength, verify_password,
)
from app.services import messaging
from app.tenancy import TenantSession

logger = logging.getLogger("gstr2b.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

GENERIC_LOGIN_ERROR = "Email or password is incorrect."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        max_age=settings.refresh_token_ttl_days * 24 * 3600,
        path="/api/auth",
        domain=settings.cookie_domain or None,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path="/api/auth",
        domain=settings.cookie_domain or None,
    )


def _issue_refresh_token(
    db: Session, user: models.User, request: Request,
) -> str:
    raw = generate_token(32)
    db.add(models.RefreshToken(
        firm_id=user.firm_id,
        user_id=user.id,
        token_hash=hash_token(raw),
        expires_at=datetime.utcnow() + timedelta(days=settings.refresh_token_ttl_days),
        user_agent=user_agent(request),
        ip_address=client_ip(request),
    ))
    return raw


def _revoke_all_refresh_tokens(db: Session, user_id: str) -> int:
    now = datetime.utcnow()
    rows = (
        db.query(models.RefreshToken)
        .filter(models.RefreshToken.user_id == user_id,
                models.RefreshToken.revoked_at.is_(None))
        .all()
    )
    for row in rows:
        row.revoked_at = now
    return len(rows)


def _token_response(db: Session, user: models.User) -> schemas.TokenOut:
    firm = db.get(models.Firm, user.firm_id)
    access = create_access_token(
        user_id=user.id,
        firm_id=user.firm_id,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        scoped_client_id=user.scoped_client_id,
    )
    return schemas.TokenOut(
        access_token=access,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=schemas.UserOut.model_validate(user),
        firm=schemas.FirmOut.model_validate(firm),
    )


def _is_locked(user: models.User) -> bool:
    return bool(user.locked_until and user.locked_until > datetime.utcnow())


# ---------------------------------------------------------------------------
# Signup / login
# ---------------------------------------------------------------------------

@router.post("/signup", response_model=schemas.TokenOut, status_code=status.HTTP_201_CREATED)
def signup(
    payload: schemas.FirmSignupIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Create a firm and its first OWNER user.

    This is the only endpoint that creates a tenant, and the only place a
    user is created without an invitation.
    """
    try:
        validate_password_strength(payload.password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    email = payload.email.lower().strip()
    if db.query(models.User).filter(models.User.email == email).first():
        # Signup is one place where enumeration is hard to avoid: the user
        # must be told the address is taken. The message deliberately does
        # not say which firm holds it.
        raise HTTPException(
            status_code=409,
            detail="An account with this email address already exists. Try signing in instead.",
        )

    firm = models.Firm(
        name=payload.firm_name.strip(),
        contact_email=email,
        contact_phone=payload.phone,
        gstin=payload.firm_gstin,
        pan=payload.firm_pan,
    )
    db.add(firm)
    db.flush()

    user = models.User(
        firm_id=firm.id,
        name=payload.full_name.strip(),
        email=email,
        phone=payload.phone,
        role=models.UserRole.OWNER,
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    db.add(user)
    db.flush()

    db.add(models.Subscription(firm_id=firm.id, tier=firm.subscription_tier,
                               status=firm.subscription_status))

    audit.record(
        db, firm_id=firm.id, user_id=user.id, action=AuditAction.FIRM_SIGNUP,
        entity_type="Firm", entity_id=firm.id,
        metadata={"firm_name": firm.name, "email": email},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )

    raw_refresh = _issue_refresh_token(db, user, request)
    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    _set_refresh_cookie(response, raw_refresh)
    return _token_response(db, user)


@router.post("/login", response_model=schemas.TokenOut)
def login(
    payload: schemas.LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    email = payload.email.lower().strip()
    user = db.query(models.User).filter(models.User.email == email).first()

    if user is None or user.deleted_at is not None:
        # Hash anyway so that a missing account and a wrong password take
        # comparable time, denying a timing-based enumeration oracle.
        hash_password(payload.password)
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

    if _is_locked(user):
        remaining = int((user.locked_until - datetime.utcnow()).total_seconds() // 60) + 1
        raise HTTPException(
            status_code=423,
            detail=f"Account is temporarily locked after repeated failed sign-in "
                   f"attempts. Try again in {remaining} minute(s).",
        )

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated.")

    if not verify_password(payload.password, user.password_hash):
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        action = AuditAction.LOGIN_FAILED
        if user.failed_login_attempts >= settings.max_failed_logins:
            user.locked_until = datetime.utcnow() + timedelta(
                minutes=settings.account_lockout_minutes
            )
            user.failed_login_attempts = 0
            action = AuditAction.ACCOUNT_LOCKED
        audit.record(
            db, firm_id=user.firm_id, user_id=user.id, action=action,
            entity_type="User", entity_id=user.id,
            metadata={"email": email},
            ip_address=client_ip(request), user_agent=user_agent(request),
        )
        db.commit()
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

    # Second factor, if the user has one.
    if user.totp_enabled:
        if not payload.totp_code:
            raise HTTPException(
                status_code=401,
                detail="A two-factor authentication code is required.",
            )
        secret = decrypt(user.totp_secret)
        # valid_window=1 tolerates one 30-second step of clock drift.
        if not pyotp.TOTP(secret).verify(payload.totp_code.strip(), valid_window=1):
            audit.record(
                db, firm_id=user.firm_id, user_id=user.id,
                action=AuditAction.LOGIN_FAILED, entity_type="User", entity_id=user.id,
                metadata={"reason": "invalid_totp"},
                ip_address=client_ip(request), user_agent=user_agent(request),
            )
            db.commit()
            raise HTTPException(status_code=401, detail="Two-factor code is incorrect.")

    # Firm policy may require 2FA for senior roles.
    firm = db.get(models.Firm, user.firm_id)
    if (firm and firm.require_2fa_for_partners and not user.totp_enabled
            and user.role in (models.UserRole.OWNER, models.UserRole.PARTNER)):
        raise HTTPException(
            status_code=403,
            detail="Your firm requires two-factor authentication for partner accounts. "
                   "Contact your firm owner to complete setup.",
        )

    # Successful login.
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = datetime.utcnow()

    if needs_rehash(user.password_hash):
        # Transparently upgrade to current hashing parameters.
        user.password_hash = hash_password(payload.password)

    raw_refresh = _issue_refresh_token(db, user, request)
    audit.record(
        db, firm_id=user.firm_id, user_id=user.id, action=AuditAction.LOGIN_SUCCESS,
        entity_type="User", entity_id=user.id,
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    db.commit()
    db.refresh(user)

    _set_refresh_cookie(response, raw_refresh)
    return _token_response(db, user)


@router.post("/refresh", response_model=schemas.TokenOut)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Exchange a refresh cookie for a new access token, rotating the cookie."""
    raw = request.cookies.get(settings.refresh_cookie_name)
    if not raw:
        raise HTTPException(status_code=401, detail="No refresh token supplied.")

    row = (
        db.query(models.RefreshToken)
        .filter(models.RefreshToken.token_hash == hash_token(raw))
        .first()
    )
    if row is None:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Refresh token is not recognised.")

    if row.revoked_at is not None:
        # Replay of an already-rotated token: treat as compromise and kill
        # every live session for this user.
        revoked = _revoke_all_refresh_tokens(db, row.user_id)
        audit.record(
            db, firm_id=row.firm_id, user_id=row.user_id,
            action=AuditAction.LOGOUT_ALL_DEVICES, entity_type="User",
            entity_id=row.user_id,
            metadata={"reason": "refresh_token_reuse_detected", "sessions_revoked": revoked},
            ip_address=client_ip(request), user_agent=user_agent(request),
        )
        db.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=401,
            detail="This session has been ended for security reasons. Please sign in again.",
        )

    if row.expires_at <= datetime.utcnow():
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Session has expired. Please sign in again.")

    user = db.get(models.User, row.user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Account is no longer active.")

    # Rotate.
    row.revoked_at = datetime.utcnow()
    new_raw = _issue_refresh_token(db, user, request)
    audit.record(
        db, firm_id=user.firm_id, user_id=user.id, action=AuditAction.TOKEN_REFRESHED,
        entity_type="User", entity_id=user.id,
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    db.commit()
    db.refresh(user)

    _set_refresh_cookie(response, new_raw)
    return _token_response(db, user)


@router.post("/logout", response_model=schemas.MessageOut)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    raw = request.cookies.get(settings.refresh_cookie_name)
    if raw:
        row = (
            db.query(models.RefreshToken)
            .filter(models.RefreshToken.token_hash == hash_token(raw))
            .first()
        )
        if row and row.revoked_at is None:
            row.revoked_at = datetime.utcnow()
            audit.record(
                db, firm_id=row.firm_id, user_id=row.user_id, action=AuditAction.LOGOUT,
                entity_type="User", entity_id=row.user_id,
                ip_address=client_ip(request), user_agent=user_agent(request),
            )
            db.commit()
    _clear_refresh_cookie(response)
    return schemas.MessageOut(message="Signed out.")


@router.post("/logout-all", response_model=schemas.MessageOut)
def logout_all_devices(
    request: Request,
    response: Response,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    count = _revoke_all_refresh_tokens(db, user.id)
    audit.record(
        db, firm_id=user.firm_id, user_id=user.id,
        action=AuditAction.LOGOUT_ALL_DEVICES, entity_type="User", entity_id=user.id,
        metadata={"sessions_revoked": count},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    db.commit()
    _clear_refresh_cookie(response)
    return schemas.MessageOut(message=f"Signed out of {count} session(s) on all devices.")


@router.get("/me", response_model=schemas.TokenOut)
def me(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Current user and firm. The access_token field echoes a fresh token."""
    return _token_response(db, user)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

@router.post("/password-reset/request", response_model=schemas.MessageOut)
def request_password_reset(
    payload: schemas.PasswordResetRequestIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Always reports success, whether or not the address is registered.

    Saying "no such account" here would turn this endpoint into a free
    membership oracle for anyone with a list of email addresses.
    """
    email = payload.email.lower().strip()
    user = db.query(models.User).filter(models.User.email == email).first()

    if user and user.is_active and user.deleted_at is None:
        raw = generate_token(32)
        db.add(models.PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=datetime.utcnow() + timedelta(
                minutes=settings.password_reset_ttl_minutes),
        ))
        audit.record(
            db, firm_id=user.firm_id, user_id=user.id,
            action=AuditAction.PASSWORD_RESET_REQUESTED,
            entity_type="User", entity_id=user.id,
            ip_address=client_ip(request), user_agent=user_agent(request),
        )
        db.commit()
        messaging.send_password_reset_email(
            to=user.email, name=user.name,
            reset_url=f"{settings.app_base_url}/reset-password?token={raw}",
        )

    return schemas.MessageOut(
        message="If that email address has an account, a reset link has been sent to it."
    )


@router.post("/password-reset/confirm", response_model=schemas.MessageOut)
def confirm_password_reset(
    payload: schemas.PasswordResetConfirmIn,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        validate_password_strength(payload.new_password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = (
        db.query(models.PasswordResetToken)
        .filter(models.PasswordResetToken.token_hash == hash_token(payload.token))
        .first()
    )
    if row is None or row.used_at is not None or row.expires_at <= datetime.utcnow():
        raise HTTPException(
            status_code=400,
            detail="This reset link is invalid or has expired. Request a new one.",
        )

    user = db.get(models.User, row.user_id)
    if user is None or user.deleted_at is not None:
        raise HTTPException(status_code=400, detail="This reset link is no longer valid.")

    user.password_hash = hash_password(payload.new_password)
    user.failed_login_attempts = 0
    user.locked_until = None
    row.used_at = datetime.utcnow()

    # A password reset invalidates every existing session, since the reset
    # may have been prompted by a suspected compromise.
    revoked = _revoke_all_refresh_tokens(db, user.id)

    audit.record(
        db, firm_id=user.firm_id, user_id=user.id,
        action=AuditAction.PASSWORD_RESET_COMPLETED,
        entity_type="User", entity_id=user.id,
        metadata={"sessions_revoked": revoked},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    db.commit()
    return schemas.MessageOut(
        message="Password updated. You have been signed out of all devices."
    )


@router.post("/password/change", response_model=schemas.MessageOut)
def change_password(
    payload: schemas.ChangePasswordIn,
    request: Request,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    try:
        validate_password_strength(payload.new_password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    user.password_hash = hash_password(payload.new_password)
    audit.record(
        db, firm_id=user.firm_id, user_id=user.id, action=AuditAction.PASSWORD_CHANGED,
        entity_type="User", entity_id=user.id,
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    db.commit()
    return schemas.MessageOut(message="Password updated.")


# ---------------------------------------------------------------------------
# Two-factor authentication
# ---------------------------------------------------------------------------

@router.post("/2fa/setup", response_model=schemas.TOTPSetupOut)
def setup_totp(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a TOTP secret. Not enabled until a code is verified."""
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already enabled.")

    secret = pyotp.random_base32()
    user.totp_secret = encrypt(secret)   # stored encrypted; enabled only after verify
    db.commit()

    firm = db.get(models.Firm, user.firm_id)
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email, issuer_name=f"GSTR-2B ({firm.name})" if firm else "GSTR-2B",
    )
    return schemas.TOTPSetupOut(secret=secret, provisioning_uri=uri)


@router.post("/2fa/verify", response_model=schemas.MessageOut)
def verify_totp(
    payload: schemas.TOTPVerifyIn,
    request: Request,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="Start two-factor setup first.")
    secret = decrypt(user.totp_secret)
    if not pyotp.TOTP(secret).verify(payload.code.strip(), valid_window=1):
        raise HTTPException(status_code=400, detail="That code is not correct. Try again.")

    user.totp_enabled = True
    audit.record(
        db, firm_id=user.firm_id, user_id=user.id, action=AuditAction.TOTP_ENABLED,
        entity_type="User", entity_id=user.id,
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    db.commit()
    return schemas.MessageOut(message="Two-factor authentication is now enabled.")


@router.post("/2fa/disable", response_model=schemas.MessageOut)
def disable_totp(
    payload: schemas.TOTPVerifyIn,
    request: Request,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.totp_enabled:
        raise HTTPException(status_code=400, detail="Two-factor authentication is not enabled.")
    secret = decrypt(user.totp_secret)
    if not pyotp.TOTP(secret).verify(payload.code.strip(), valid_window=1):
        raise HTTPException(status_code=400, detail="That code is not correct.")

    firm = db.get(models.Firm, user.firm_id)
    if (firm and firm.require_2fa_for_partners
            and user.role in (models.UserRole.OWNER, models.UserRole.PARTNER)):
        raise HTTPException(
            status_code=403,
            detail="Your firm requires two-factor authentication for partner accounts.",
        )

    user.totp_enabled = False
    user.totp_secret = None
    audit.record(
        db, firm_id=user.firm_id, user_id=user.id, action=AuditAction.TOTP_DISABLED,
        entity_type="User", entity_id=user.id,
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    db.commit()
    return schemas.MessageOut(message="Two-factor authentication disabled.")


# ---------------------------------------------------------------------------
# Staff invitations
# ---------------------------------------------------------------------------

@router.post("/invitations", response_model=schemas.InvitationOut,
             status_code=status.HTTP_201_CREATED)
def invite_user(
    payload: schemas.InviteUserIn,
    request: Request,
    inviter: models.User = Depends(require_role(Role.PARTNER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Invite a colleague. Partner or Owner only.

    An invitation carries the role, so accepting it cannot grant more
    authority than the inviter chose.
    """
    db = tenant.db
    email = payload.email.lower().strip()

    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(
            status_code=409,
            detail="Someone with that email address already has an account.",
        )

    existing = (
        tenant.query(models.Invitation)
        .filter(models.Invitation.email == email,
                models.Invitation.accepted_at.is_(None),
                models.Invitation.revoked_at.is_(None),
                models.Invitation.expires_at > datetime.utcnow())
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="An invitation for that address is already pending.",
        )

    if payload.scoped_client_id:
        # Validates that the client belongs to this firm.
        tenant.get_or_404(models.Client, payload.scoped_client_id, "Client not found")
    if payload.role == models.UserRole.CLIENT_VIEWER and not payload.scoped_client_id:
        raise HTTPException(
            status_code=422,
            detail="A client viewer must be scoped to exactly one client.",
        )

    raw = generate_token(32)
    invitation = models.Invitation(
        email=email,
        role=payload.role,
        token_hash=hash_token(raw),
        invited_by=inviter.id,
        expires_at=datetime.utcnow() + timedelta(days=settings.invitation_ttl_days),
    )
    tenant.add(invitation)

    firm = db.get(models.Firm, tenant.firm_id)
    audit.record(
        db, firm_id=tenant.firm_id, user_id=inviter.id, action=AuditAction.USER_INVITED,
        entity_type="Invitation", entity_id=invitation.id,
        metadata={"email": email, "role": payload.role.value},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    db.commit()
    db.refresh(invitation)

    messaging.send_invitation_email(
        to=email,
        firm_name=firm.name if firm else "your firm",
        inviter_name=inviter.name,
        role=payload.role.value,
        accept_url=f"{settings.app_base_url}/accept-invite?token={raw}",
    )
    return invitation


@router.get("/invitations", response_model=list[schemas.InvitationOut])
def list_invitations(
    _: models.User = Depends(require_role(Role.MANAGER)),
    tenant: TenantSession = Depends(get_tenant),
):
    return (
        tenant.query(models.Invitation)
        .order_by(models.Invitation.created_at.desc())
        .all()
    )


@router.delete("/invitations/{invitation_id}", response_model=schemas.MessageOut)
def revoke_invitation(
    invitation_id: str,
    request: Request,
    actor: models.User = Depends(require_role(Role.PARTNER)),
    tenant: TenantSession = Depends(get_tenant),
):
    invitation = tenant.get_or_404(models.Invitation, invitation_id, "Invitation not found")
    if invitation.accepted_at:
        raise HTTPException(status_code=409, detail="That invitation has already been accepted.")
    invitation.revoked_at = datetime.utcnow()
    audit.record(
        tenant.db, firm_id=tenant.firm_id, user_id=actor.id,
        action=AuditAction.INVITATION_REVOKED,
        entity_type="Invitation", entity_id=invitation.id,
        ip_address=client_ip(request), user_agent=user_agent(request),
    )
    tenant.commit()
    return schemas.MessageOut(message="Invitation revoked.")


@router.post("/invitations/accept", response_model=schemas.TokenOut)
def accept_invitation(
    payload: schemas.AcceptInviteIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Accept an invitation and create the user.

    Unauthenticated by necessity: the firm is identified by the invitation
    token, which is the one place a firm_id legitimately comes from outside
    a session. The token is single-use and time-limited.
    """
    try:
        validate_password_strength(payload.password)
    except PasswordPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    invitation = (
        db.query(models.Invitation)
        .filter(models.Invitation.token_hash == hash_token(payload.token))
        .first()
    )
    if invitation is None or invitation.revoked_at is not None:
        raise HTTPException(status_code=400, detail="This invitation is not valid.")
    if invitation.accepted_at is not None:
        raise HTTPException(status_code=409, detail="This invitation has already been used.")
    if invitation.expires_at <= datetime.utcnow():
        raise HTTPException(
            status_code=400,
            detail="This invitation has expired. Ask your firm to send a new one.",
        )
    if db.query(models.User).filter(models.User.email == invitation.email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    inviter = db.get(models.User, invitation.invited_by)
    scoped_client_id = None
    if invitation.role == models.UserRole.CLIENT_VIEWER and inviter:
        scoped_client_id = None  # set by the firm afterwards if not on the invite

    user = models.User(
        firm_id=invitation.firm_id,
        name=payload.full_name.strip(),
        email=invitation.email,
        phone=payload.phone,
        role=invitation.role,
        password_hash=hash_password(payload.password),
        is_active=True,
        email_verified=True,   # proven by possession of the emailed token
        scoped_client_id=scoped_client_id,
    )
    db.add(user)
    db.flush()

    invitation.accepted_at = datetime.utcnow()
    audit.record(
        db, firm_id=invitation.firm_id, user_id=user.id,
        action=AuditAction.INVITATION_ACCEPTED,
        entity_type="User", entity_id=user.id,
        metadata={"invitation_id": invitation.id, "role": str(invitation.role)},
        ip_address=client_ip(request), user_agent=user_agent(request),
    )

    raw_refresh = _issue_refresh_token(db, user, request)
    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(user)

    _set_refresh_cookie(response, raw_refresh)
    return _token_response(db, user)


# ---------------------------------------------------------------------------
# Firm staff
# ---------------------------------------------------------------------------

@router.get("/users", response_model=list[schemas.UserOut])
def list_firm_users(
    _: models.User = Depends(require_role(Role.ASSOCIATE)),
    tenant: TenantSession = Depends(get_tenant),
):
    return tenant.query(models.User).order_by(models.User.created_at.asc()).all()


@router.get("/audit-log", response_model=list[schemas.AuditLogOut])
def read_audit_log(
    limit: int = 100,
    _: models.User = Depends(require_role(Role.PARTNER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """The firm's own audit trail. Partner and above."""
    return (
        tenant.query(models.AuditLog)
        .order_by(models.AuditLog.created_at.desc())
        .limit(min(limit, 500))
        .all()
    )


@router.get("/audit-log/verify")
def verify_audit_log(
    _: models.User = Depends(require_role(Role.PARTNER)),
    tenant: TenantSession = Depends(get_tenant),
):
    """Recompute the tamper-evident hash chain and report any break."""
    return audit.verify_chain(tenant.db, tenant.firm_id)
