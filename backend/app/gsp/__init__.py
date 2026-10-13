"""
GSP integration layer.

Callers import `get_gsp_provider` and the interface from here and never name a
concrete adapter. Adding ClearTax or MasterGST means writing one class and one
branch in `get_gsp_provider`.
"""
import logging
import threading
from typing import Optional

from app.config import settings
from app.gsp.base import (  # noqa: F401  (re-exported)
    ConsentExpiredError, ConsentGrant, GSPCapabilityError, GSPConfigurationError,
    GSPError, GSPProvider, GSPRateLimitedError, GSPResponseError,
    GSPUnavailableError, GSPValidationError, GSTINInfo, GSTINNotFoundError,
    GSTR1Result, GSTR2BResult, InvalidOTPError, OTPRequestError, OTPRequestResult,
    ReturnNotAvailableError,
)

logger = logging.getLogger("gstr2b.gsp")

_lock = threading.Lock()
_sandbox_instance: Optional[GSPProvider] = None


def reset_provider_cache() -> None:
    """Forget the cached adapter. Tests use it after changing settings."""
    global _sandbox_instance
    with _lock:
        _sandbox_instance = None


def get_gsp_provider() -> GSPProvider:
    global _sandbox_instance
    name = (settings.gsp_provider or "").strip().lower()

    if name == "mock":
        if settings.environment != "development":
            raise GSPConfigurationError(
                f"GSP_PROVIDER=mock is not permitted when ENVIRONMENT={settings.environment}. "
                "It returns fabricated filings. Set GSP_PROVIDER=sandbox and supply "
                "credentials."
            )
        from app.gsp.mock import MockGSPProvider
        return MockGSPProvider(session_hours=settings.mock_gsp_session_hours)

    if name == "sandbox":
        # One instance, so its 24-hour application token is cached across
        # requests instead of re-authenticating on every call.
        with _lock:
            if _sandbox_instance is None:
                from app.gsp.sandbox import SandboxAdapter
                _sandbox_instance = SandboxAdapter(
                    base_url=settings.sandbox_base_url,
                    api_key=settings.sandbox_api_key,
                    api_secret=settings.sandbox_api_secret,
                    api_version=settings.sandbox_api_version,
                    timeout=settings.gsp_http_timeout_seconds,
                    gstr2b_year_basis=settings.sandbox_gstr2b_year_basis,
                )
            return _sandbox_instance

    raise GSPConfigurationError(
        f"Unknown GSP_PROVIDER {settings.gsp_provider!r}. Available: mock, sandbox."
    )
