"""
The GST Suvidha Provider (GSP) contract.

Everything above this layer, meaning the consent flow, the scheduled fetch,
reconciliation and the notifications, talks to a `GSPProvider` and to nothing
else. Swapping Sandbox for ClearTax or MasterGST means writing one class that
implements this interface. No calling code and no part of the matching engine
changes, which is the point of the boundary.

Two design decisions shape it:

**Providers are stateless about consent.** Methods that act for a taxpayer
receive the access token as an argument. Storing, encrypting, refreshing and
expiring that token is the platform's job (app/services/consent.py), not the
adapter's. An adapter that cached tokens internally would hide consent state
from the code that has to alert people about it.

**Failures carry a policy, not just a message.** Every error declares whether
it is worth retrying and what a human should be told. The scheduled job never
inspects error text: it asks `error.retryable`. That keeps "GSTN is briefly
down, try again in a minute" and "the client's consent has expired, stop and
tell someone" from being confused with one another.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GSTINInfo:
    gstin: str
    legal_name: Optional[str]
    trade_name: Optional[str]
    status: str                  # as reported: "Active", "Cancelled", "Suspended"...
    state_name: Optional[str] = None
    constitution: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_active(self) -> bool:
        return (self.status or "").strip().lower() == "active"


@dataclass(frozen=True)
class OTPRequestResult:
    """OTP requested. The OTP goes to the taxpayer's registered contact on the
    GST portal, never through us, so there is nothing to carry back except a
    human-readable confirmation and the provider's transaction id."""
    message: str
    transaction_id: Optional[str] = None


@dataclass(frozen=True)
class ConsentGrant:
    """A live authorisation to read a taxpayer's returns.

    Two expiries, because they are genuinely different things:

      token_expires_at    when THIS access token stops working. Short: 6 hours
                          for Sandbox. Extended by `refresh_consent` without
                          bothering the taxpayer.
      session_expires_at  when the whole consent ends and an OTP is needed
                          again. Set by the taxpayer on the GST portal, from 6
                          hours to 30 days. This is "the consent window".

    Both come from the provider's response. Neither is ever assumed.
    """
    access_token: str = field(repr=False)     # never printed, even by accident
    token_expires_at: datetime
    session_expires_at: datetime
    transaction_id: Optional[str] = None


@dataclass(frozen=True)
class GSTR2BResult:
    """A fetched GSTR-2B, already normalised to the platform's shape.

    `document` matches what `app.ingestion.gstr2b.parse_gstr2b_json` expects,
    so live data and manual uploads take exactly the same path from here on.
    """
    document: dict
    raw: dict = field(repr=False)             # the provider's response, kept for audit
    invoice_count: int = 0
    credit_note_count: int = 0
    pages: int = 1
    transaction_id: Optional[str] = None
    # Anything in the response we could not use, e.g. an invoice with no number.
    warnings: list = field(default_factory=list)


@dataclass(frozen=True)
class GSTR1Result:
    gstin: str
    period: str
    invoices: list                             # outward supplies to registered buyers (B2B)
    raw: dict = field(repr=False)
    transaction_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GSPError(Exception):
    """Base class. `retryable` is the contract the scheduler relies on."""

    retryable = False
    code = "GSP_ERROR"

    def __init__(self, message: str, *, transaction_id: Optional[str] = None,
                 provider_code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.transaction_id = transaction_id
        self.provider_code = provider_code

    def __str__(self) -> str:
        return self.message


class GSPUnavailableError(GSPError):
    """GSTN or the GSP is down, slow, or unreachable. Transient: try again."""
    retryable = True
    code = "GSP_UNAVAILABLE"


class GSPRateLimitedError(GSPError):
    retryable = True
    code = "GSP_RATE_LIMITED"

    def __init__(self, message: str, *, retry_after: Optional[int] = None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class ReturnNotAvailableError(GSPError):
    """GSTR-2B has not been generated yet, which is normal in the early hours
    of the 14th. Retryable, because it resolves by itself."""
    retryable = True
    code = "RETURN_NOT_AVAILABLE"


class ConsentExpiredError(GSPError):
    """The taxpayer's session is over or was revoked. Only a fresh OTP fixes
    it, so retrying is pointless and a person must be told."""
    retryable = False
    code = "CONSENT_EXPIRED"


class InvalidOTPError(GSPError):
    code = "INVALID_OTP"


class OTPRequestError(GSPError):
    """The OTP could not be issued: unknown portal username, too many active
    sessions for this GSP account, and so on."""
    code = "OTP_REQUEST_FAILED"


class GSTINNotFoundError(GSPError):
    code = "GSTIN_NOT_FOUND"


class GSPValidationError(GSPError):
    """The provider rejected our request as malformed. A bug or bad input on
    our side; retrying the same request will fail the same way."""
    code = "GSP_VALIDATION"


class GSPConfigurationError(GSPError):
    """Our own credentials or setup are wrong. Not the client's problem and not
    fixable by the associate, so the message says to contact support."""
    code = "GSP_CONFIGURATION"


class GSPResponseError(GSPError):
    """A response arrived but did not have the shape we rely on."""
    code = "GSP_BAD_RESPONSE"


class GSPCapabilityError(GSPError):
    """The provider does not support this operation."""
    code = "GSP_UNSUPPORTED"


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class GSPProvider(ABC):
    name = "abstract"

    # Providers that can extend a session without an OTP set this True.
    supports_refresh = False

    @abstractmethod
    def verify_gstin(self, gstin: str) -> GSTINInfo:
        """Look a GSTIN up on the public register."""

    @abstractmethod
    def initiate_otp_consent(self, gstin: str, username: str) -> OTPRequestResult:
        """Ask the GST portal to send an OTP to the taxpayer's registered
        contact. `username` is the taxpayer's GST portal login, which some
        GSPs need to route the OTP; adapters that do not need it ignore it."""

    @abstractmethod
    def verify_otp_consent(self, gstin: str, username: str, otp: str) -> ConsentGrant:
        """Exchange the OTP for a live access token and its expiries."""

    def refresh_consent(self, access_token: str) -> ConsentGrant:
        """Extend a live session without an OTP, up to the session ceiling."""
        raise GSPCapabilityError(f"{self.name} cannot refresh a session without an OTP.")

    @abstractmethod
    def fetch_gstr2b(self, gstin: str, period: str, access_token: str) -> GSTR2BResult:
        """Fetch GSTR-2B for `period` ("YYYY-MM") as the platform's document."""

    @abstractmethod
    def fetch_gstr1(self, gstin: str, period: str, access_token: str) -> GSTR1Result:
        """Fetch the outward-supply (B2B) invoices the taxpayer filed."""

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "supports_refresh": self.supports_refresh}
