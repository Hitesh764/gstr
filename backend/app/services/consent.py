"""
The lifecycle of a client's GST portal consent.

A consent is a *session*, and it has two clocks:

    token    the current access token. Short (6 hours for Sandbox). Renewed
             silently by `ensure_fresh_token` while the session is alive.
    session  the consent window. Chosen by the taxpayer on the GST portal
             (6 hours to 30 days). When it ends, only a fresh OTP restores
             access, and a person has to be told.

Neither is assumed. Both are stored exactly as the provider reported them.

States, derived rather than stored so they cannot go stale:

    NONE      never connected, or explicitly revoked. The manual upload path is
              the whole story for this GSTIN, and nobody is nagged about it.
    ACTIVE    a live session.
    EXPIRING  live, but the window closes soon enough that someone should renew.
    EXPIRED   the window ended. Automatic fetching is blocked until renewed.

The token is encrypted at rest and only ever decrypted at the moment of use.
"""
import logging
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Optional

from app import models
from app.config import settings
from app.crypto import decrypt, encrypt
from app.gsp import ConsentExpiredError, ConsentGrant, GSPError, GSPProvider

logger = logging.getLogger("gstr2b.consent")

IST_OFFSET = timedelta(hours=5, minutes=30)

# "Expiring" means the smaller of a day and a quarter of the whole window, so a
# 6-hour consent is not perpetually "expiring" and a 30-day one warns in time.
EXPIRING_MAX = timedelta(hours=24)
EXPIRING_FRACTION = 0.25


class ConsentState(str, Enum):
    NONE = "NONE"
    ACTIVE = "ACTIVE"
    EXPIRING = "EXPIRING"
    EXPIRED = "EXPIRED"


def _now() -> datetime:
    return datetime.utcnow()


def to_ist(now: datetime) -> datetime:
    """The scheduled window is defined in Indian calendar days, while the
    database holds naive UTC."""
    return now + IST_OFFSET


def consent_state(g: models.ClientGSTIN, now: Optional[datetime] = None) -> ConsentState:
    now = now or _now()
    if g.consent_granted_at is None or g.consent_expires_at is None:
        return ConsentState.NONE
    if g.consent_expires_at <= now:
        return ConsentState.EXPIRED

    remaining = g.consent_expires_at - now
    window = g.consent_expires_at - g.consent_granted_at
    threshold = min(EXPIRING_MAX, window * EXPIRING_FRACTION)
    return ConsentState.EXPIRING if remaining <= threshold else ConsentState.ACTIVE


def is_live(g: models.ClientGSTIN, now: Optional[datetime] = None) -> bool:
    return consent_state(g, now) in (ConsentState.ACTIVE, ConsentState.EXPIRING)


def has_token(g: models.ClientGSTIN) -> bool:
    return bool(g.gsp_consent_token)


def read_token(g: models.ClientGSTIN) -> Optional[str]:
    """Decrypt the stored token, or None if there is none.

    A token that cannot be decrypted means the ENCRYPTION_KEY has changed since
    it was stored. Silently treating that as "no consent" would look identical
    to never having connected, so it is raised as an expiry with a reason that
    points at the real cause.
    """
    if not g.gsp_consent_token:
        return None
    try:
        return decrypt(g.gsp_consent_token)
    except ValueError as exc:
        raise ConsentExpiredError(
            "The stored GST portal credential could not be read, which usually means "
            "the encryption key changed. Connect again with a new OTP."
        ) from exc


def store_grant(
    g: models.ClientGSTIN, grant: ConsentGrant, *, provider: str,
    username: Optional[str] = None, first_grant: bool = False,
    now: Optional[datetime] = None,
) -> None:
    """Record a grant or a refresh. Does not commit."""
    now = now or _now()
    g.gsp_consent_token = encrypt(grant.access_token)
    g.gsp_token_expires_at = grant.token_expires_at
    g.consent_expires_at = grant.session_expires_at
    g.gsp_provider = provider
    g.gsp_last_refreshed_at = now
    if username:
        g.gst_portal_username = username
    if first_grant or g.consent_granted_at is None:
        g.consent_granted_at = now


def drop_token(g: models.ClientGSTIN, now: Optional[datetime] = None) -> None:
    """Forget a dead credential but keep the record that consent existed.

    The state must read EXPIRED (someone is told) rather than NONE ("never
    connected", nobody is told). If the provider reports the session over
    EARLIER than the end date we recorded, for instance because the taxpayer
    revoked access on the GST portal, the recorded end is pulled forward to now.
    Otherwise the state would still read ACTIVE with no token behind it, and the
    next alert would claim access ended on a date in the future.
    """
    now = now or _now()
    g.gsp_consent_token = None
    g.gsp_token_expires_at = None
    if g.consent_expires_at is None or g.consent_expires_at > now:
        g.consent_expires_at = now


def revoke(g: models.ClientGSTIN) -> None:
    """Explicit disconnection: back to NONE."""
    g.gsp_consent_token = None
    g.gsp_token_expires_at = None
    g.consent_granted_at = None
    g.consent_expires_at = None
    g.gsp_last_refreshed_at = None


def token_needs_refresh(g: models.ClientGSTIN, now: Optional[datetime] = None) -> bool:
    now = now or _now()
    if g.gsp_token_expires_at is None:
        return False
    margin = timedelta(minutes=settings.gsp_token_refresh_margin_minutes)
    return g.gsp_token_expires_at <= now + margin


def ensure_fresh_token(
    g: models.ClientGSTIN, provider: GSPProvider, now: Optional[datetime] = None,
) -> str:
    """Return a usable access token, refreshing it first if it is close to dying.

    Raises ConsentExpiredError when the session is over, in which case a new OTP
    is the only remedy. Does not commit; the caller owns the transaction.
    """
    now = now or _now()

    state = consent_state(g, now)
    if state is ConsentState.NONE:
        raise ConsentExpiredError("This GSTIN is not connected to the GST portal.")
    if state is ConsentState.EXPIRED:
        raise ConsentExpiredError(
            "The GST portal consent for this GSTIN has ended. Connect again with a new OTP.")

    # A token issued by one provider is meaningless to another, and sending it
    # would at best fail and at worst leak it to a third party.
    if g.gsp_provider and g.gsp_provider != provider.name:
        raise ConsentExpiredError(
            f"This GSTIN was connected through '{g.gsp_provider}', but the platform is now "
            f"using '{provider.name}'. Connect again.")

    token = read_token(g)
    if token is None:
        raise ConsentExpiredError("No access token is stored for this GSTIN. Connect again.")

    if not token_needs_refresh(g, now):
        return token

    if not provider.supports_refresh:
        if g.gsp_token_expires_at and g.gsp_token_expires_at <= now:
            raise ConsentExpiredError("The access token has expired. Connect again.")
        return token

    try:
        grant = provider.refresh_consent(token)
    except ConsentExpiredError:
        drop_token(g, now)
        raise
    except GSPError as exc:
        # A transient fault while refreshing should not fail a fetch that the
        # current token can still serve.
        if g.gsp_token_expires_at and g.gsp_token_expires_at > now:
            logger.warning("Token refresh failed (%s); continuing with the live token.", exc)
            return token
        raise

    store_grant(g, grant, provider=provider.name, now=now)
    return grant.access_token


# ---------------------------------------------------------------------------
# The scheduled fetch window
# ---------------------------------------------------------------------------

def fetch_window_days() -> list[int]:
    days = sorted({int(d) for d in settings.gstr2b_fetch_days.split(",") if d.strip().isdigit()})
    return days or [14]


def next_fetch_window(now: Optional[datetime] = None) -> tuple[date, date]:
    """The current or next scheduled fetch window as (first day, last day), in
    Indian calendar days. Fetching starts on the 14th because that is when
    GSTN generates GSTR-2B."""
    today = to_ist(now or _now()).date()
    days = fetch_window_days()
    first, last = days[0], days[-1]
    if today.day <= last:
        y, m = today.year, today.month
    else:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, min(first, 28)), date(y, m, min(last, 28))


def needs_renewal_before_window(
    g: models.ClientGSTIN, now: Optional[datetime] = None, days_ahead: int = 5,
) -> Optional[date]:
    """If this consent will not still be live when the next fetch window closes,
    return that window's first day. Otherwise None.

    This is what makes hands-off monthly fetching dependable. A 30-day consent
    granted on the 20th lapses on the 20th of next month, in the middle of the
    following window, so it is flagged a few days ahead rather than found dead
    on the morning it was needed.
    """
    now = now or _now()
    if consent_state(g, now) in (ConsentState.NONE,):
        return None
    start, end = next_fetch_window(now)
    today = to_ist(now).date()
    if today < start - timedelta(days=days_ahead):
        return None
    window_close_utc = datetime(end.year, end.month, end.day, 23, 59, 59) - IST_OFFSET
    if g.consent_expires_at is not None and g.consent_expires_at < window_close_utc:
        return start
    return None


def humanize(delta: timedelta) -> str:
    """'6 hours', '3 days': for messages a person will read."""
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "now"
    if seconds < 3600:
        return f"{max(1, seconds // 60)} minutes"
    if seconds < 172800:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = seconds // 86400
    return f"{days} days"
