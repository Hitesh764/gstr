"""
Password hashing and JWT issuing/verification.

Passwords use Argon2id, the current password-hashing recommendation. The
verifier also reports when a stored hash was produced with older parameters
so it can be transparently upgraded on the user's next successful login.

Access tokens are short-lived (15 minutes by default) and carry the claims
the API needs for authorisation: the user, their firm, and their role. The
firm claim is what makes tenant scoping cheap, but it is still verified
against the database on every request — see app/deps.py — because a token
issued before a role change or a deactivation must not outlive it.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from app.config import settings

# Defaults are argon2-cffi's recommended parameters.
_hasher = PasswordHasher()

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish verification. Returns False rather than raising."""
    try:
        _hasher.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the hash used weaker parameters than the current policy."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, VerificationError):
        return False


class PasswordPolicyError(ValueError):
    pass


def validate_password_strength(password: str) -> None:
    """Minimum viable policy. Raises PasswordPolicyError with a usable message.

    Length is the dominant factor in password strength, so the floor is 10
    characters rather than an elaborate character-class rule that pushes
    people toward predictable substitutions.
    """
    if len(password) < 10:
        raise PasswordPolicyError("Password must be at least 10 characters long.")
    if len(password) > 256:
        raise PasswordPolicyError("Password must be at most 256 characters long.")
    if password.isdigit():
        raise PasswordPolicyError("Password cannot be only digits.")
    if password.lower() in {
        "password12", "password123", "letmein123", "qwerty1234", "admin12345",
    }:
        raise PasswordPolicyError("That password is too common. Choose another.")


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(
    *,
    user_id: str,
    firm_id: str,
    role: str,
    scoped_client_id: Optional[str] = None,
    expires_minutes: Optional[int] = None,
) -> str:
    expires_minutes = expires_minutes or settings.access_token_ttl_minutes
    issued = _now()
    payload: dict[str, Any] = {
        "sub": user_id,
        "firm_id": firm_id,
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(minutes=expires_minutes)).timestamp()),
    }
    if scoped_client_id:
        payload["scoped_client_id"] = scoped_client_id
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


class TokenError(Exception):
    """Raised for any invalid, expired or wrong-type token."""


def decode_access_token(token: str) -> dict:
    """Decode and validate an access token, or raise TokenError."""
    try:
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Access token is invalid.") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        # A refresh token must never be usable as an access token.
        raise TokenError("Token is not an access token.")
    if not payload.get("sub") or not payload.get("firm_id"):
        raise TokenError("Token is missing required claims.")
    return payload
