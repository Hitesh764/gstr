"""
Tenant-scoped data access.

The rule this module enforces: **`firm_id` is never accepted from client
input.** It is derived from the authenticated session and applied as a
mandatory filter on every read and a mandatory value on every write.

Routes do not receive a raw SQLAlchemy `Session`. They receive a
`TenantSession`, which cannot express an unscoped query against a
tenant-owned table. The difference matters because the alternative — a raw
session plus a remembered `.filter_by(firm_id=...)` in every route — fails
the first time someone forgets, and that failure is silent and serves one
firm's financial data to another.

The escape hatch (`unscoped()`) exists because a few operations genuinely
must cross tenants: authenticating a user before their firm is known,
resolving an invitation token, and platform-admin tooling. It is a
deliberately ugly name so that its use is visible in review.
"""
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterable, Optional, Type, TypeVar

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Query, Session

from app import models

T = TypeVar("T")

_TENANT_MODELS = set(models.TENANT_SCOPED_MODELS)
_REFERENCE_MODELS = {models.AuditStandardReference, models.CAROClauseDefinition,
                     models.CAROApplicabilityRule}


class TenantScopeError(RuntimeError):
    """Raised when a query would escape, or has no, tenant scope."""


def is_tenant_scoped(model: Type[Any]) -> bool:
    return model in _TENANT_MODELS


def supports_soft_delete(model: Type[Any]) -> bool:
    return hasattr(model, "deleted_at")


class TenantSession:
    """A SQLAlchemy session bound to exactly one firm.

    Every method that touches a tenant-owned table applies
    `firm_id == self.firm_id`. Attempting to query a tenant-owned model
    without a firm is a programming error and raises rather than silently
    returning everything.
    """

    def __init__(self, db: Session, firm_id: str, user: Optional[models.User] = None):
        if not firm_id:
            raise TenantScopeError(
                "TenantSession requires a firm_id derived from the authenticated user."
            )
        self.db = db
        self.firm_id = firm_id
        self.user = user

    # -- reads ------------------------------------------------------------

    def query(self, model: Type[T], *, include_deleted: bool = False) -> Query:
        """A query pre-filtered to this firm, excluding soft-deleted rows."""
        q = self.db.query(model)
        if is_tenant_scoped(model):
            q = q.filter(model.firm_id == self.firm_id)
        elif model is not models.Firm:
            raise TenantScopeError(
                f"{model.__name__} is not tenant-scoped and is not Firm. Add it to "
                "models.TENANT_SCOPED_MODELS, or use unscoped() deliberately."
            )
        else:
            # The firm row itself: scope by primary key.
            q = q.filter(models.Firm.id == self.firm_id)

        if not include_deleted and supports_soft_delete(model):
            q = q.filter(model.deleted_at.is_(None))
        return q

    def get(self, model: Type[T], obj_id: str, *, include_deleted: bool = False) -> Optional[T]:
        """Fetch one row by id **within this firm**.

        Returns None for a row belonging to another firm, so a caller that
        forgets to check gets a 404 rather than another tenant's record.
        This is why routes must never call `db.get()` directly.
        """
        if not obj_id:
            return None
        return self.query(model, include_deleted=include_deleted).filter(
            model.id == obj_id
        ).one_or_none()

    def get_or_404(self, model: Type[T], obj_id: str, detail: Optional[str] = None) -> T:
        from fastapi import HTTPException
        obj = self.get(model, obj_id)
        if obj is None:
            raise HTTPException(
                status_code=404,
                detail=detail or f"{model.__name__} not found",
            )
        return obj

    def query_reference(self, model: Type[T]) -> Query:
        """Read global versioned audit reference data explicitly.

        These catalogs have no firm_id by design. They are allowlisted here
        for reads only; writes remain the responsibility of curated data
        loading/migrations, never regular firm-facing API requests.
        """
        if model not in _REFERENCE_MODELS:
            raise TenantScopeError(
                f"{model.__name__} is not an allowlisted global reference model."
            )
        return self.db.query(model)

    def get_reference(self, model: Type[T], obj_id: str) -> Optional[T]:
        if model not in _REFERENCE_MODELS:
            raise TenantScopeError(
                f"{model.__name__} is not an allowlisted global reference model."
            )
        return self.db.get(model, obj_id)

    # -- writes -----------------------------------------------------------

    def add(self, obj: Any) -> Any:
        """Add a row, stamping firm_id from the session.

        If the object already carries a different firm_id, that is a bug or
        an attack, and it raises instead of being quietly corrected.
        """
        model = type(obj)
        if is_tenant_scoped(model):
            existing = getattr(obj, "firm_id", None)
            if existing and existing != self.firm_id:
                raise TenantScopeError(
                    f"Refusing to write {model.__name__} belonging to firm "
                    f"{existing!r} from a session scoped to {self.firm_id!r}."
                )
            obj.firm_id = self.firm_id
        self.db.add(obj)
        return obj

    def add_all(self, objs: Iterable[Any]) -> None:
        for obj in objs:
            self.add(obj)

    def soft_delete(self, obj: Any, retention_days: Optional[int] = None) -> Any:
        """Flag a row as deleted rather than destroying it."""
        model = type(obj)
        if not supports_soft_delete(model):
            raise TenantScopeError(f"{model.__name__} does not support soft delete.")
        if is_tenant_scoped(model) and getattr(obj, "firm_id", None) != self.firm_id:
            raise TenantScopeError("Refusing to delete a row belonging to another firm.")
        obj.deleted_at = datetime.utcnow()
        if retention_days is not None:
            from datetime import timedelta
            obj.retention_until = datetime.utcnow() + timedelta(days=retention_days)
        return obj

    # -- passthrough ------------------------------------------------------

    def flush(self) -> None:
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def refresh(self, obj: Any) -> None:
        self.db.refresh(obj)

    @contextmanager
    def unscoped(self, reason: str):
        """Escape hatch for the few genuinely cross-tenant operations.

        Yields the raw session. Named to be conspicuous in review; `reason`
        is required so the intent is written down at the call site.

            with tenant.unscoped("resolve invitation token") as db:
                ...
        """
        if not reason:
            raise TenantScopeError("unscoped() requires a stated reason.")
        yield self.db


def assert_model_registry_complete() -> list[str]:
    """Report tenant-owned tables missing from TENANT_SCOPED_MODELS.

    Any mapped class with a `firm_id` column must be listed, otherwise
    `TenantSession.query` would reject it and, worse, a developer might
    reach for the raw session instead. Called by a test so that adding a
    model without registering it fails CI rather than in production.
    """
    missing = []
    for mapper in models.Base.registry.mappers:
        cls = mapper.class_
        columns = {c.key for c in sa_inspect(cls).columns}
        if "firm_id" in columns and cls not in _TENANT_MODELS:
            missing.append(cls.__name__)
    return sorted(missing)
