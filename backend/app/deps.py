"""
Request-scoped dependencies: authentication, tenant scope, role checks.

The chain every protected route goes through:

    Authorization header
      -> decode + validate the access token        (security.decode_access_token)
      -> load the user from the database           (never trust claims alone)
      -> build a TenantSession bound to their firm (app/tenancy.py)
      -> optionally assert a role                  (require_role / RoleChecker)

The database lookup on every request is deliberate. A JWT is a snapshot of
who the user was when it was issued. Between issue and expiry someone may
have been deactivated, had their role reduced, or had their firm suspended.
A 15-minute window of stale authority is not acceptable for a system that
approves financial filings, so the claims are re-checked against current
state rather than trusted on their own.
"""
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.security import TokenError, decode_access_token
from app.tenancy import TenantSession

# auto_error=False so a missing header produces our own 401 with a useful
# message rather than FastAPI's bare "Not authenticated".
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> models.User:
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Not authenticated. Supply a Bearer access token.")

    try:
        payload = decode_access_token(credentials.credentials)
    except TokenError as exc:
        raise _unauthorized(str(exc)) from exc

    user = db.get(models.User, payload["sub"])

    # Re-validate against current state, not the token's snapshot.
    if user is None or user.deleted_at is not None:
        raise _unauthorized("Account no longer exists.")
    if not user.is_active:
        raise _unauthorized("Account has been deactivated.")
    if user.firm_id != payload.get("firm_id"):
        # The firm claim disagreeing with the stored row means either a
        # tampered token or a moved user. Neither should proceed.
        raise _unauthorized("Token does not match the account's firm.")

    firm = db.get(models.Firm, user.firm_id)
    if firm is None or firm.deleted_at is not None:
        raise _unauthorized("Firm account is no longer active.")

    return user


def get_tenant(
    request: Request,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TenantSession:
    """The dependency routes should use for all data access.

    `firm_id` comes from the authenticated user, never from the path, query
    string or body.
    """
    tenant = TenantSession(db=db, firm_id=user.firm_id, user=user)
    # Stashed for audit helpers that need request metadata.
    request.state.tenant = tenant
    request.state.user = user
    return tenant


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

Role = models.UserRole

# Ordered by authority. A check for MANAGER admits PARTNER and OWNER.
_ROLE_RANK = {
    Role.CLIENT_VIEWER: 0,
    Role.ASSOCIATE: 1,
    Role.MANAGER: 2,
    Role.PARTNER: 3,
    Role.OWNER: 4,
}


def role_rank(role) -> int:
    if isinstance(role, str):
        role = Role(role)
    return _ROLE_RANK.get(role, -1)


def require_role(minimum: Role) -> Callable:
    """Dependency factory asserting at least `minimum` authority.

        @router.post("/clients", dependencies=[Depends(require_role(Role.MANAGER))])
    """
    def _check(user: models.User = Depends(get_current_user)) -> models.User:
        if role_rank(user.role) < role_rank(minimum):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"This action requires the "
                    f"{minimum.value.replace('_', ' ').title()} role or higher. "
                    f"Your role is {Role(user.role).value.replace('_', ' ').title()}."
                ),
            )
        return user
    return _check


def require_exact_roles(*roles: Role) -> Callable:
    """Assert membership of a specific set, where rank ordering does not apply."""
    allowed = {r.value for r in roles}

    def _check(user: models.User = Depends(get_current_user)) -> models.User:
        current = user.role.value if hasattr(user.role, "value") else str(user.role)
        if current not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action is restricted to: {', '.join(sorted(allowed))}.",
            )
        return user
    return _check


def deny_client_viewer(user: models.User = Depends(get_current_user)) -> models.User:
    """Block the read-only client role from any mutating endpoint."""
    if role_rank(user.role) <= role_rank(Role.CLIENT_VIEWER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client viewers have read-only access.",
        )
    return user


def assert_client_visible(user: models.User, client_id: str) -> None:
    """A CLIENT_VIEWER may only see the one client they are scoped to.

    Firm staff see every client in their firm; tenancy has already
    constrained that to their own firm.
    """
    if role_rank(user.role) == role_rank(Role.CLIENT_VIEWER):
        if not user.scoped_client_id or user.scoped_client_id != client_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this client.",
            )


# ---------------------------------------------------------------------------
# Request metadata (for audit entries)
# ---------------------------------------------------------------------------

def client_ip(request: Request) -> Optional[str]:
    """Best-effort client IP.

    X-Forwarded-For is only meaningful behind a proxy that sets it; a
    direct client can forge it, so it must not be used for authorisation.
    Recorded for audit context only.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host[:64] if request.client else None


def user_agent(request: Request) -> Optional[str]:
    ua = request.headers.get("user-agent")
    return ua[:400] if ua else None
