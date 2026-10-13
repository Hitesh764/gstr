"""
Sandbox.co.in adapter.

Written against Sandbox's published API reference (developer.sandbox.co.in),
not against memory of it. The points that are easy to get wrong, and are
handled deliberately below:

* **Two credentials.** The application token comes from `POST /authenticate`
  (24 hours, from an API key and secret). The taxpayer token comes from the OTP
  step (6 hours, per taxpayer). Both are sent in the `authorization` header,
  and neither takes a `Bearer` prefix: Sandbox says its token "is NOT a bearer
  token". Every call also carries `x-api-key`.

* **Failures arrive as HTTP 200.** A wrong OTP, a missing return or a GST
  portal fault comes back with `data.status_cd == "0"` and an `error_cd`, so a
  status-code check alone reports success on a failure. `_check` reads the body.

* **Consent is a session with a ceiling, not a fixed lifetime.** Every taxpayer
  token lasts 6 hours. The taxpayer picks how long the overall session may last
  on the GST portal (6 hours to 30 days), and `session/refresh` extends tokens
  without an OTP until that ceiling. Both expiries are read from the response.

* **GSTR-2B can paginate.** `status_cd == "3"` plus a file count `fc` means
  fetch `file_number` 1..fc and merge.

Anything Sandbox's documentation left ambiguous is flagged `# UNVERIFIED` so it
is easy to find and confirm against a real sandbox account.

Nothing in this file logs a token, an OTP, a secret or a request body.
"""
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.gsp import gstn_format
from app.gsp.base import (
    ConsentExpiredError, ConsentGrant, GSPConfigurationError, GSPError,
    GSPProvider, GSPRateLimitedError, GSPResponseError, GSPUnavailableError,
    GSPValidationError, GSTINInfo, GSTINNotFoundError, GSTR1Result, GSTR2BResult,
    InvalidOTPError, OTPRequestError, OTPRequestResult, ReturnNotAvailableError,
)

logger = logging.getLogger("gstr2b.gsp.sandbox")


class _RedactOTPFilter(logging.Filter):
    """Blanks the OTP in URLs before any handler sees them.

    Sandbox requires the OTP as a QUERY parameter (`.../otp/verify?otp=575757`),
    and httpx logs every request URL at INFO. This adapter never logs the OTP
    itself, but the HTTP library underneath it would, putting single-use
    authentication codes into whatever collects the logs. Redacting keeps the
    request log useful without keeping the secret.
    """
    _pattern = re.compile(r"([?&]otp=)[^&\s\"]+", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._pattern.sub(r"\1***", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


for _name in ("httpx", "httpcore"):
    _lg = logging.getLogger(_name)
    if not any(isinstance(f, _RedactOTPFilter) for f in _lg.filters):
        _lg.addFilter(_RedactOTPFilter())

# Sandbox's application token lasts 24 hours. Renew comfortably before that.
APP_TOKEN_MAX_AGE_SECONDS = 23 * 3600

# A runaway page count from a malformed response must not become a runaway loop.
MAX_GSTR2B_PAGES = 200

# Error codes seen in Sandbox's documentation.
ERR_INVALID_SESSION = "AUTH4033"      # bad OTP on verify; dead session elsewhere
ERR_MAX_SESSIONS = "AUTH403"
ERR_OTP_SERVER_DOWN = "TEC4001"
ERR_GSTIN_NOT_FOUND = "FO8000"
ERR_2B_NOT_AVAILABLE = "RET2B1023"
ERR_2B_NO_DETAILS = "RET2B1016"


def _ms_to_utc(value: Any) -> Optional[datetime]:
    """Sandbox timestamps are Unix epoch milliseconds. The platform stores
    naive UTC datetimes, so convert and drop the tzinfo."""
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class SandboxAdapter(GSPProvider):
    name = "sandbox"
    supports_refresh = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        api_version: str = "1.0.0",
        timeout: float = 30.0,
        gstr2b_year_basis: str = "financial",
        client: Optional[httpx.Client] = None,
        monotonic=time.monotonic,
    ):
        self._base = base_url.rstrip("/")
        self._api_key = api_key
        self._api_secret = api_secret
        self._version = api_version
        self._year_basis = gstr2b_year_basis
        # Connect fast, read patiently: GSTN can be slow to answer.
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(timeout, connect=10.0)
        )
        self._monotonic = monotonic
        self._app_token: Optional[str] = None
        self._app_token_at: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _headers(self, token: Optional[str], *, json_body: bool = False,
                 source: Optional[str] = None) -> dict:
        h = {
            "accept": "application/json",
            "x-api-key": self._api_key,
            "x-api-version": self._version,
        }
        if token:
            h["authorization"] = token          # deliberately no "Bearer"
        if json_body:
            h["content-type"] = "application/json"
        if source:
            h["x-source"] = source
        return h

    def _send(self, method: str, path: str, *, token: Optional[str], taxpayer: bool,
              params: Optional[dict] = None, json: Optional[dict] = None,
              source: Optional[str] = None, context: str = "") -> dict:
        """One HTTP round trip, returning the parsed body or raising GSPError."""
        url = f"{self._base}{path}"
        try:
            response = self._client.request(
                method, url, params=params, json=json,
                headers=self._headers(token, json_body=json is not None, source=source),
            )
        except httpx.TimeoutException as exc:
            raise GSPUnavailableError("The GST service did not respond in time.") from exc
        except httpx.TransportError as exc:
            raise GSPUnavailableError("Could not reach the GST service.") from exc

        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = None
        txn = body.get("transaction_id") if isinstance(body, dict) else None
        logger.debug("sandbox %s %s -> %s (txn %s)", method, path, status, txn)

        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise GSPRateLimitedError(
                "The GST service is rate limiting requests.",
                retry_after=int(retry_after) if retry_after and retry_after.isdigit() else None,
                transaction_id=txn,
            )
        if status >= 500:
            raise GSPUnavailableError(
                f"The GST service returned an error ({status}).", transaction_id=txn
            )
        if status in (401, 403):
            if taxpayer:
                raise ConsentExpiredError(
                    "GST portal access is no longer valid. A new OTP is needed.",
                    transaction_id=txn,
                )
            raise GSPConfigurationError(
                "Sandbox rejected our API credentials. Contact support.", transaction_id=txn
            )
        if status == 422 or (400 <= status < 500):
            message = (body or {}).get("message") if isinstance(body, dict) else None
            raise GSPValidationError(
                message or f"The request was rejected ({status}).", transaction_id=txn
            )
        if not isinstance(body, dict):
            raise GSPResponseError("The GST service returned a response we could not read.",
                                   transaction_id=txn)

        self._check(body, taxpayer=taxpayer, context=context)
        return body

    def _check(self, body: dict, *, taxpayer: bool, context: str) -> None:
        """Translate an in-body failure (HTTP 200, status_cd "0") into an error."""
        code = body.get("code")
        if isinstance(code, int) and code >= 400:
            raise GSPValidationError(body.get("message") or f"Rejected ({code}).",
                                     transaction_id=body.get("transaction_id"))

        data = body.get("data")
        if not isinstance(data, dict) or str(data.get("status_cd")) not in ("0",):
            return
        error = data.get("error") or {}
        self._raise_provider_error(
            str(error.get("error_cd") or ""), str(error.get("message") or ""),
            txn=body.get("transaction_id"), context=context,
        )

    def _raise_provider_error(self, code: str, message: str, *, txn: Optional[str],
                              context: str) -> None:
        kw = {"transaction_id": txn, "provider_code": code or None}
        if code == ERR_INVALID_SESSION:
            if context == "verify_otp":
                raise InvalidOTPError("That OTP is incorrect or has expired.", **kw)
            raise ConsentExpiredError(
                "GST portal access has expired. A new OTP is needed.", **kw)
        if code == ERR_MAX_SESSIONS:
            raise OTPRequestError(
                "This GSP account already has the maximum number of active sessions "
                "for this taxpayer. Wait for one to end, then try again.", **kw)
        if code == ERR_GSTIN_NOT_FOUND:
            raise GSTINNotFoundError("No registration was found for that GSTIN.", **kw)
        if code == ERR_2B_NOT_AVAILABLE:
            raise ReturnNotAvailableError(
                "GSTR-2B for this period has not been generated yet.", **kw)
        # UNVERIFIED: GSTN error codes prefixed "TEC" denote technical faults, and
        # the one documented example (TEC4001, OTP server down) is transient. All
        # TEC codes are treated as retryable on that basis.
        if code.startswith("TEC"):
            raise GSPUnavailableError(message or "The GST portal reported a technical fault.", **kw)
        raise GSPResponseError(
            f"{message or 'The GST service reported an error'}"
            f"{f' (code {code})' if code else ''}.", **kw)

    # ------------------------------------------------------------------
    # Application token
    # ------------------------------------------------------------------

    def _application_token(self, *, force: bool = False) -> str:
        if not (self._api_key and self._api_secret):
            raise GSPConfigurationError(
                "Sandbox credentials are not configured. Set SANDBOX_API_KEY and "
                "SANDBOX_API_SECRET."
            )
        with self._lock:
            fresh = (self._app_token is not None
                     and self._monotonic() - self._app_token_at < APP_TOKEN_MAX_AGE_SECONDS)
            if fresh and not force:
                return self._app_token  # type: ignore[return-value]

            try:
                response = self._client.post(
                    f"{self._base}/authenticate",
                    headers={
                        "accept": "application/json",
                        "x-api-key": self._api_key,
                        "x-api-secret": self._api_secret,
                        "x-api-version": self._version,
                    },
                )
            except httpx.TimeoutException as exc:
                raise GSPUnavailableError("The GST service did not respond in time.") from exc
            except httpx.TransportError as exc:
                raise GSPUnavailableError("Could not reach the GST service.") from exc

            if response.status_code in (401, 403):
                raise GSPConfigurationError(
                    "Sandbox rejected our API key or secret. Contact support.")
            if response.status_code >= 500:
                raise GSPUnavailableError("The GST service is unavailable.")
            try:
                token = response.json()["data"]["access_token"]
            except (ValueError, KeyError, TypeError) as exc:
                raise GSPResponseError(
                    "Sandbox did not return an access token.") from exc

            self._app_token, self._app_token_at = token, self._monotonic()
            return token

    def _app_call(self, method: str, path: str, **kw) -> dict:
        """A call authorised by the application token, re-authenticating once
        if Sandbox says the cached token has gone stale."""
        try:
            return self._send(method, path, token=self._application_token(),
                              taxpayer=False, **kw)
        except GSPConfigurationError:
            # The cached token may simply have expired early. One fresh attempt.
            return self._send(method, path, token=self._application_token(force=True),
                              taxpayer=False, **kw)

    # ------------------------------------------------------------------
    # GSPProvider
    # ------------------------------------------------------------------

    def verify_gstin(self, gstin: str) -> GSTINInfo:
        body = self._app_call("POST", "/gst/compliance/public/gstin/search",
                              json={"gstin": gstin}, context="verify_gstin")
        try:
            info = body["data"]["data"]
        except (KeyError, TypeError) as exc:
            raise GSPResponseError("The GSTIN lookup response was not in the expected shape.",
                                   transaction_id=body.get("transaction_id")) from exc
        state = ((info.get("pradr") or {}).get("addr") or {}).get("stcd")
        return GSTINInfo(
            gstin=str(info.get("gstin") or gstin).upper(),
            legal_name=info.get("lgnm"),
            trade_name=info.get("tradeNam"),
            status=str(info.get("sts") or "Unknown"),
            state_name=state,
            constitution=info.get("ctb"),
            raw=info,
        )

    def initiate_otp_consent(self, gstin: str, username: str) -> OTPRequestResult:
        body = self._app_call(
            "POST", "/gst/compliance/tax-payer/otp",
            json={"username": username, "gstin": gstin}, source="primary",
            context="request_otp",
        )
        return OTPRequestResult(
            message="An OTP has been sent to the mobile number or email registered "
                    f"on the GST portal for user '{username}'.",
            transaction_id=body.get("transaction_id"),
        )

    def verify_otp_consent(self, gstin: str, username: str, otp: str) -> ConsentGrant:
        body = self._app_call(
            "POST", "/gst/compliance/tax-payer/otp/verify",
            params={"otp": otp}, json={"username": username, "gstin": gstin},
            source="primary", context="verify_otp",
        )
        return self._grant_from(body)

    def refresh_consent(self, access_token: str) -> ConsentGrant:
        body = self._send(
            "POST", "/gst/compliance/tax-payer/session/refresh",
            token=access_token, taxpayer=True, context="refresh",
        )
        return self._grant_from(body)

    def _grant_from(self, body: dict) -> ConsentGrant:
        txn = body.get("transaction_id")
        data = body.get("data") or {}
        token = data.get("access_token")
        token_exp = _ms_to_utc(data.get("token_expiry"))
        session_exp = _ms_to_utc(data.get("session_expiry"))
        if not token:
            raise GSPResponseError("Sandbox did not return a taxpayer access token.",
                                   transaction_id=txn)
        if token_exp is None and session_exp is None:
            # Refuse rather than invent a lifetime: the whole point is that the
            # window comes from the provider.
            raise GSPResponseError(
                "Sandbox did not report when this access expires.", transaction_id=txn)
        # If only one expiry is given, use it for both. Treating the session as
        # ending when the token does can only understate consent, never overstate it.
        token_exp = token_exp or session_exp
        session_exp = session_exp or token_exp
        if session_exp < token_exp:
            session_exp = token_exp   # a session cannot end before its live token
        return ConsentGrant(access_token=token, token_expires_at=token_exp,
                            session_expires_at=session_exp, transaction_id=txn)

    # ------------------------------------------------------------------
    # Returns
    # ------------------------------------------------------------------

    def _gstr2b_path(self, period: str) -> str:
        if self._year_basis == "calendar":
            year = int(period.split("-")[0])
        else:
            year = gstn_format.financial_year_start(period)
        return f"/gst/compliance/tax-payer/gstrs/gstr-2b/{year}/{gstn_format.period_to_mmyyyy(period)}"

    def fetch_gstr2b(self, gstin: str, period: str, access_token: str) -> GSTR2BResult:
        path = self._gstr2b_path(period)
        try:
            first = self._send("GET", path, token=access_token, taxpayer=True,
                               context="gstr2b")
        except GSPResponseError as exc:
            if exc.provider_code == ERR_2B_NO_DETAILS:
                return self._empty_statement(gstin, period, exc)
            raise

        data = first.get("data") or {}
        status_cd = str(data.get("status_cd"))
        txn = first.get("transaction_id")

        if status_cd == "3":
            # UNVERIFIED: where `fc` sits in the paginated response. Both the
            # outer and inner envelope are checked.
            fc = data.get("fc") or (data.get("data") or {}).get("fc")
            try:
                page_count = int(fc)
            except (TypeError, ValueError):
                raise GSPResponseError(
                    "GSTR-2B is paginated but the response did not say how many pages.",
                    transaction_id=txn)
            if not 1 <= page_count <= MAX_GSTR2B_PAGES:
                raise GSPResponseError(
                    f"GSTR-2B reported an implausible page count ({page_count}).",
                    transaction_id=txn)
            # UNVERIFIED: whether the status-3 response itself carries data. It is
            # discarded and pages 1..fc are fetched, which cannot double count.
            pages = [
                self._send("GET", path, token=access_token, taxpayer=True,
                           params={"file_number": n}, context="gstr2b")
                for n in range(1, page_count + 1)
            ]
        elif status_cd == "1":
            pages = [first]
        else:
            raise GSPResponseError(
                f"GSTR-2B returned an unrecognised status ({status_cd}).", transaction_id=txn)

        document, n_inv, n_notes, warnings = gstn_format.normalize_gstr2b(
            pages, gstin=gstin, period=period)
        return GSTR2BResult(
            document=document, raw={"pages": pages}, invoice_count=n_inv,
            credit_note_count=n_notes, pages=len(pages), transaction_id=txn,
            warnings=warnings,
        )

    def _empty_statement(self, gstin: str, period: str, exc: GSPError) -> GSTR2BResult:
        """GSTN reports there is nothing to show for the period. That is a real
        answer (no supplier reported anything), not a failure."""
        return GSTR2BResult(
            document={"gstin": gstin.upper(), "period": period, "generated_on": None,
                      "invoices": [], "credit_notes": []},
            raw={"pages": [], "note": "no_details_available"},
            invoice_count=0, credit_note_count=0, pages=0,
            transaction_id=exc.transaction_id,
            warnings=["GSTN reports no GSTR-2B details for this period."],
        )

    def fetch_gstr1(self, gstin: str, period: str, access_token: str) -> GSTR1Result:
        year, month = (int(x) for x in period.split("-"))
        # GSTR-1 endpoints take the calendar year and a plain month, unlike GSTR-2B.
        # UNVERIFIED: zero-padding of the month (the docs' only example is "12").
        body = self._send(
            "GET", f"/gst/compliance/tax-payer/gstrs/gstr-1/b2b/{year}/{month:02d}",
            token=access_token, taxpayer=True, context="gstr1",
        )
        invoices = gstn_format.normalize_gstr1_b2b(body, gstin=gstin, period=period)
        return GSTR1Result(gstin=gstin.upper(), period=period, invoices=invoices,
                           raw=body, transaction_id=body.get("transaction_id"))
