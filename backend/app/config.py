"""
Application configuration — environment driven, no hardcoded secrets.

Values are read from (in order of precedence):
  1. real environment variables
  2. the .env file at backend/.env  (see .env.example)
  3. the defaults declared below

Local development runs on SQLite with zero setup. Staging/production
override DATABASE_URL with a PostgreSQL URL; nothing else needs to change.

The matching tolerances are re-exported as module-level constants so the
matching engine keeps importing them by name and stays free of any
settings/framework dependency.
"""
import os
import secrets
import warnings
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # backend/
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

SAMPLE_DATA_DIR = os.path.join(BASE_DIR, "sample_data")

DEFAULT_SQLITE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'gstr2b.db')}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(BASE_DIR, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Environment ---
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = True

    @field_validator("debug", mode="before")
    @classmethod
    def normalize_debug_setting(cls, value):
        # Some launchers export DEBUG=release/development as an environment
        # label. Pydantic's strict boolean parser rejects those strings and
        # prevents the API and Alembic from starting at all.
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "production", "prod", "false", "0", "off", "no"}:
                return False
            if normalized in {"development", "dev", "debug", "true", "1", "on", "yes"}:
                return True
        return value

    # --- Database ---
    # SQLite by default so a local clone runs with no external services.
    # Override with e.g. postgresql+psycopg://user:pass@host:5432/gstr2b
    database_url: str = DEFAULT_SQLITE_URL

    # --- API / CORS ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    # Comma-separated in .env; parsed into a list below.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Matching engine tolerances ---
    exact_match_value_tolerance: float = 1.0      # rupees
    formatting_value_tolerance: float = 10.0      # rupees
    formatting_date_tolerance_days: int = 3
    similar_invoice_number_ratio: float = 0.92
    value_mismatch_date_window_days: int = 10

    # --- Secrets ---
    # Deliberately empty by default. In development a throwaway key is
    # generated at import time (with a warning) so a fresh clone runs; in
    # staging/production an empty value is a hard startup failure, because
    # a shared default signing key is indistinguishable from no auth.
    secret_key: str = ""
    encryption_key: str = ""

    # --- Auth / JWT ---
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    password_reset_ttl_minutes: int = 60
    invitation_ttl_days: int = 7

    # Brute-force protection
    max_failed_logins: int = 5
    account_lockout_minutes: int = 15

    # Refresh-token cookie
    refresh_cookie_name: str = "gstr2b_refresh"
    # `secure` must be False over plain http://localhost or the browser
    # silently drops the cookie; it is forced True outside development.
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    cookie_domain: str = ""

    # --- Rate limiting ---
    rate_limit_default: str = "200/minute"
    rate_limit_auth: str = "10/minute"

    # --- Outbound messaging ---
    # "console" writes the message to the log instead of sending it, which
    # keeps local development free of third-party accounts. Swap to a real
    # provider by changing these once credentials exist.
    # "console" logs the message; "resend" sends through Resend's API.
    email_provider: str = "console"
    sms_provider: str = "console"
    resend_api_key: str = ""
    email_from: str = "no-reply@gstr2b.local"
    app_base_url: str = "http://localhost:5173"

    # --- GST Suvidha Provider (live GST portal access) ---
    # "mock" needs no credentials and returns bundled sample data, so the whole
    # connect-and-fetch flow can be exercised locally. It is refused outside
    # development, because serving fake filings to a real client would be far
    # worse than failing to start.
    gsp_provider: str = "mock"           # "mock" | "sandbox"
    gsp_http_timeout_seconds: float = 30.0

    # Sandbox.co.in. Test host: https://test-api.sandbox.co.in
    sandbox_base_url: str = "https://api.sandbox.co.in"
    sandbox_api_key: str = ""
    sandbox_api_secret: str = ""
    sandbox_api_version: str = "1.0.0"
    # Sandbox documents the GSTR-2B `year` path parameter as the FINANCIAL year
    # (2024 for Nov 2024 and for Feb 2025), whereas its GSTR-1 endpoints use
    # the calendar year. The documentation has one example only, so this is
    # a switch rather than a hardcoded guess. Flip it if live calls for
    # January to March return "not available".
    sandbox_gstr2b_year_basis: str = "financial"     # "financial" | "calendar"

    # OTP consent
    gsp_otp_ttl_minutes: int = 10
    gsp_otp_max_verify_attempts: int = 3
    gsp_otp_max_requests_per_hour: int = 5      # protects the GST portal from OTP spam
    # Refresh a taxpayer token (6h life) when it has less than this left.
    gsp_token_refresh_margin_minutes: int = 90
    mock_gsp_session_hours: int = 168

    # --- Background jobs (Celery + Redis) ---
    celery_broker_url: str = "redis://127.0.0.1:6379/0"
    celery_timezone: str = "Asia/Kolkata"
    # GSTR-2B for month M is generated on the 14th of month M+1. The job runs on
    # each listed day and skips GSTINs already fetched, so the 15th and 16th
    # act as a second and third chance rather than duplicate work.
    gstr2b_fetch_days: str = "14,15,16"
    gstr2b_fetch_hour: int = 6
    gstr2b_fetch_max_attempts: int = 6

    # --- File storage (uploaded notices) ---
    # Files are encrypted before they are written, whichever backend holds them.
    # "local" keeps them on this machine's disk. An S3-compatible adapter slots
    # in behind the same interface once a provider is chosen.
    storage_backend: str = "local"                 # "local" | "s3" (not built yet)
    storage_local_dir: str = os.path.join(DATA_DIR, "files")
    max_notice_upload_mb: int = 15

    # --- Notice analysis ---
    # "mock" needs no credentials: a rule-based reader good enough to exercise
    # the whole workflow locally. It is refused outside development, because a
    # keyword matcher reading a real statutory notice would be dangerous.
    notice_ai_provider: str = "mock"               # "mock" | "claude"
    anthropic_api_key: str = ""
    notice_ai_model: str = "claude-opus-5"
    notice_ai_timeout_seconds: float = 120.0
    notice_ai_max_tokens: int = 16000
    # Longest notice text sent to the model, in characters. Anything longer is
    # refused rather than silently truncated: a cut-off notice can drop the
    # very paragraph that states the deadline.
    notice_ai_max_input_chars: int = 200_000
    # Most reconciliation records included as evidence for a drafted response.
    notice_evidence_max_items: int = 60

    # --- Audit drafting ---
    # Template mode keeps local development deterministic. Claude mode is
    # opt-in and requires ANTHROPIC_API_KEY; every prompt/response is stored.
    audit_ai_provider: Literal["template", "claude"] = "template"
    audit_ai_model: str = "claude-sonnet-4-6"
    audit_ai_timeout_seconds: float = 90.0
    audit_ai_max_tokens: int = 2400

    # Notice deadline alerts fire at these many days before the deadline (and
    # once more when it is due or overdue). Comma-separated.
    notice_alert_days: str = "7,3,1"
    notice_alert_hour: int = 8

    @field_validator("database_url")
    @classmethod
    def _no_empty_db_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("DATABASE_URL must not be empty")
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    s = Settings()

    if not s.secret_key:
        if s.environment == "development":
            # Ephemeral: regenerated on every restart, so sessions do not
            # survive a reload. That is intentional — it makes the missing
            # key obvious without blocking a fresh clone from running.
            s.secret_key = secrets.token_urlsafe(48)
            warnings.warn(
                "SECRET_KEY is not set; generated a temporary development key. "
                "Sessions will not survive a restart. Set SECRET_KEY in backend/.env.",
                RuntimeWarning, stacklevel=2,
            )
        else:
            raise RuntimeError(
                f"SECRET_KEY must be set when ENVIRONMENT={s.environment}. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )

    if not s.encryption_key:
        if s.environment == "development":
            from cryptography.fernet import Fernet
            s.encryption_key = Fernet.generate_key().decode()
            warnings.warn(
                "ENCRYPTION_KEY is not set; generated a temporary development key. "
                "Anything encrypted now becomes unreadable after a restart. "
                "Set ENCRYPTION_KEY in backend/.env.",
                RuntimeWarning, stacklevel=2,
            )
        else:
            raise RuntimeError(
                f"ENCRYPTION_KEY must be set when ENVIRONMENT={s.environment}. "
                "Generate one with: python -c "
                "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )

    if s.environment != "development":
        # Auth cookies must never travel over plain HTTP outside local dev.
        s.cookie_secure = True

    return s


settings = get_settings()

# --- Backwards-compatible module-level constants -------------------------
# The matching engine imports these by name. Keeping them here means the
# engine never imports the settings object or pydantic at all.
DATABASE_URL = settings.database_url

EXACT_MATCH_VALUE_TOLERANCE = settings.exact_match_value_tolerance
FORMATTING_VALUE_TOLERANCE = settings.formatting_value_tolerance
FORMATTING_DATE_TOLERANCE_DAYS = settings.formatting_date_tolerance_days
SIMILAR_INVOICE_NUMBER_RATIO = settings.similar_invoice_number_ratio
VALUE_MISMATCH_DATE_WINDOW_DAYS = settings.value_mismatch_date_window_days
