"""
FastAPI application entrypoint.

Schema ownership: tables are created and migrated by Alembic
(`alembic upgrade head`), not by `create_all` at startup. Auto-creating
tables on boot hides schema drift, which becomes dangerous once migrations
carry data transformations. The startup check reports a missing schema
loudly instead of papering over it.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy import inspect

from app.config import settings
from app.database import engine
from app.tenancy import TenantScopeError

logging.basicConfig(
    level=logging.INFO if settings.debug else logging.WARNING,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("gstr2b")

limiter = Limiter(key_func=get_remote_address, default_limits=[settings.rate_limit_default])


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        tables = inspect(engine).get_table_names()
    except Exception as exc:
        logger.error("Could not connect to the database at startup: %s", exc)
        if not settings.is_sqlite:
            logger.error(
                "Is the database running? For local development, from the project "
                "root:\n    docker compose up -d postgres\n"
                "(Docker Desktop must be running.)"
            )
        tables = []

    if settings.gsp_provider.lower() == "mock" and settings.environment != "development":
        logger.error(
            "GSP_PROVIDER=mock with ENVIRONMENT=%s. Live GST access will refuse to run, "
            "because the mock returns fabricated filings. Set GSP_PROVIDER=sandbox.",
            settings.environment,
        )

    if not tables:
        logger.warning(
            "No tables found in the database. Run migrations before using the API:\n"
            "    alembic upgrade head\n"
            "then optionally seed sample data with:\n"
            "    python scripts/seed.py"
        )
    else:
        logger.info(
            "Connected to %s database (%d tables), environment=%s.",
            "SQLite" if settings.is_sqlite else "PostgreSQL",
            len(tables), settings.environment,
        )
    yield


app = FastAPI(
    title="GST Practice Automation API",
    description=(
        "Multi-tenant GSTR-2B reconciliation, exception management and "
        "compliance tracking for CA firms."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(set(settings.cors_origin_list + (
        ["http://localhost:5173", "http://127.0.0.1:5173"]
        if settings.environment == "development" else []
    ))),
    allow_credentials=True,   # required for the httpOnly refresh cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Baseline hardening headers.

    HSTS is only sent outside development, because asserting it on
    http://localhost would pin the browser to HTTPS for localhost and
    break every other local project on that origin.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not settings.is_production:
        return response
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(TenantScopeError)
async def tenant_scope_handler(request: Request, exc: TenantScopeError):
    """A tenant-scope violation is a bug, not a user error.

    It is logged loudly and reported as a generic 500, because the detail
    would describe another firm's data.
    """
    logger.error("Tenant scope violation on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "environment": settings.environment,
        "database": "sqlite" if settings.is_sqlite else "postgresql",
        "version": app.version,
    }


from app.routers import (  # noqa: E402
    audit_brief, audit_engagements, audit_assessments, audit_ageing, audit_bank_confirmations, audit_caro_responses, audit_documents, audit_ratios, audit_related_parties, audit_policy, audit_references, auth, clients,
    exceptions, export, gsp, gstr1, notices, notifications, trial_balances,
    portfolio, reconciliation, uploads,
)

app.include_router(auth.router)
app.include_router(clients.router)
app.include_router(uploads.router)
app.include_router(gstr1.router)
app.include_router(reconciliation.router)
app.include_router(exceptions.router)
app.include_router(notifications.router)
app.include_router(gsp.router)
app.include_router(notices.router)
app.include_router(portfolio.router)
app.include_router(audit_brief.router)
app.include_router(audit_engagements.router)
app.include_router(audit_policy.router)
app.include_router(audit_references.router)
app.include_router(trial_balances.router)
app.include_router(audit_assessments.router)
app.include_router(audit_ageing.router)
app.include_router(audit_bank_confirmations.router)
app.include_router(audit_ratios.router)
app.include_router(audit_related_parties.router)
app.include_router(audit_caro_responses.router)
app.include_router(audit_documents.router)
app.include_router(export.router)
