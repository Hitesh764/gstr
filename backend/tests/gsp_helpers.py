"""
Test doubles and builders for the GSP consent and fetch tests.

`FakeGSP` is a scripted provider. Tests dial in exactly the behaviour they want
(a wrong OTP, a transient outage, an expired session) and then inspect `calls`,
which is how a test proves the platform did NOT contact the provider when it
should not have.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from app import models
from app.gsp.base import (
    ConsentExpiredError, ConsentGrant, GSPProvider, GSTINInfo, GSTR1Result, GSTR2BResult,
    InvalidOTPError, OTPRequestResult,
)
from app.security import hash_password
from app.services import consent as consent_svc

GSTIN = "27AAPFU0939F1ZV"
VENDOR = "29AAGCB7383J1Z4"
VENDOR_2 = "07AAACH1925Q1ZS"
PERIOD = "2026-03"
OTP = "123456"
TEST_PASSWORD = "test-password-123"


class FakeGSP(GSPProvider):
    name = "fake"
    supports_refresh = True

    def __init__(self):
        self.calls: list[tuple] = []
        self.otp = OTP
        self.session_hours = 168
        self.token_hours = 6
        self._counter = 0
        # Behaviour switches. An exception instance is raised; a list is
        # consumed one item per call, so a test can script "fail twice, then work".
        self.initiate_error = None
        self.verify_error = None
        self.refresh_error = None
        self.fetch_error = None
        self.document: Optional[dict] = None
        self.credit_notes = 0
        self.raw: dict = {"pages": []}

    # -- helpers -------------------------------------------------------------
    def _next(self, switch):
        if isinstance(switch, list):
            return switch.pop(0) if switch else None
        return switch

    def _grant(self, now: Optional[datetime] = None) -> ConsentGrant:
        now = now or datetime.utcnow()
        self._counter += 1
        session_end = now + timedelta(hours=self.session_hours)
        return ConsentGrant(
            access_token=f"fake-token-{self._counter}",
            token_expires_at=min(now + timedelta(hours=self.token_hours), session_end),
            session_expires_at=session_end,
            transaction_id=f"txn-{self._counter}",
        )

    # -- interface -----------------------------------------------------------
    def verify_gstin(self, gstin):
        self.calls.append(("verify_gstin", gstin))
        return GSTINInfo(gstin=gstin, legal_name="Fake Legal Pvt Ltd", trade_name="Fake Trade",
                         status="Active", state_name="Maharashtra")

    def initiate_otp_consent(self, gstin, username):
        self.calls.append(("initiate", gstin, username))
        err = self._next(self.initiate_error)
        if err:
            raise err
        return OTPRequestResult(message="OTP sent (fake).", transaction_id="txn-otp")

    def verify_otp_consent(self, gstin, username, otp):
        self.calls.append(("verify", gstin, username, otp))
        err = self._next(self.verify_error)
        if err:
            raise err
        if otp != self.otp:
            raise InvalidOTPError("That OTP is incorrect or has expired.")
        return self._grant()

    def refresh_consent(self, access_token):
        self.calls.append(("refresh", access_token))
        err = self._next(self.refresh_error)
        if err:
            raise err
        return self._grant()

    def fetch_gstr2b(self, gstin, period, access_token):
        self.calls.append(("fetch_gstr2b", gstin, period, access_token))
        err = self._next(self.fetch_error)
        if err:
            raise err
        doc = self.document or make_document(gstin, period)
        return GSTR2BResult(
            document=doc, raw=self.raw, invoice_count=len(doc["invoices"]),
            credit_note_count=self.credit_notes, pages=1, transaction_id="txn-2b",
        )

    def fetch_gstr1(self, gstin, period, access_token):
        self.calls.append(("fetch_gstr1", gstin, period))
        return GSTR1Result(gstin=gstin, period=period, invoices=[], raw={})

    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


def make_invoice(number="INV-001", vendor=VENDOR, taxable=10000.0, day=5, itc=True, period=PERIOD):
    cgst = round(taxable * 0.09, 2)
    return {
        "vendor_name": "Bharat Steel", "vendor_gstin": vendor, "invoice_number": number,
        "invoice_date": f"{period}-{day:02d}", "taxable_value": taxable,
        "cgst": cgst, "sgst": cgst, "igst": 0.0, "total_value": round(taxable + 2 * cgst, 2),
        "itc_available": itc, "filing_status": "FILED",
    }


def make_document(gstin=GSTIN, period=PERIOD, invoices=None) -> dict:
    return {"gstin": gstin, "period": period, "generated_on": "2026-04-14",
            "invoices": invoices if invoices is not None else [make_invoice()],
            "credit_notes": []}


# ---------------------------------------------------------------------------
# Database builders
# ---------------------------------------------------------------------------

class World:
    """One firm with an owner, a manager, an associate, a client and a GSTIN."""

    def __init__(self, db, slug="w", gstin=GSTIN, connected=True, provider="fake",
                 session_hours=168, assign_associate=True, now=None):
        self.db = db
        self.firm = models.Firm(name=f"Firm {slug}", contact_email=f"o@{slug}-ca.in")
        db.add(self.firm)
        db.flush()
        pw = hash_password(TEST_PASSWORD)
        self.users = {}
        for role, label in [(models.UserRole.OWNER, "owner"), (models.UserRole.MANAGER, "manager"),
                            (models.UserRole.ASSOCIATE, "associate")]:
            u = models.User(firm_id=self.firm.id, name=f"{label.title()} {slug}",
                            email=f"{label}@{slug}-ca.in", role=role, password_hash=pw,
                            email_verified=True)
            db.add(u)
            db.flush()
            self.users[label] = u
        self.client = models.Client(
            firm_id=self.firm.id, name=f"Client {slug} Ltd", status=models.ClientStatus.ACTIVE,
            assigned_associate_id=self.users["associate"].id if assign_associate else None,
        )
        db.add(self.client)
        db.flush()
        self.gstin = models.ClientGSTIN(firm_id=self.firm.id, client_id=self.client.id,
                                        gstin=gstin, state_code=gstin[:2], is_primary=True)
        db.add(self.gstin)
        db.flush()
        if connected:
            self.connect(provider=provider, session_hours=session_hours, now=now)
        db.commit()

    def connect(self, provider="fake", session_hours=168, token_hours=6, now=None):
        now = now or datetime.utcnow()
        session_end = now + timedelta(hours=session_hours)
        consent_svc.store_grant(
            self.gstin,
            ConsentGrant(access_token="stored-token", token_expires_at=min(
                now + timedelta(hours=token_hours), session_end),
                session_expires_at=session_end),
            provider=provider, username="portal.user", first_grant=True, now=now,
        )

    def add_purchase_register(self, period=PERIOD, invoices=None):
        upload = models.PurchaseRegisterUpload(
            firm_id=self.firm.id, client_gstin_id=self.gstin.id, period=period, version=1,
            is_current=True, status=models.UploadStatus.IMPORTED,
        )
        self.db.add(upload)
        self.db.flush()
        for inv in (invoices if invoices is not None else [make_invoice()]):
            self.db.add(models.PurchaseRegisterEntry(
                firm_id=self.firm.id, upload_id=upload.id, vendor_name=inv["vendor_name"],
                vendor_gstin=inv["vendor_gstin"], invoice_number=inv["invoice_number"],
                invoice_date=datetime.fromisoformat(inv["invoice_date"]).date(),
                taxable_value=Decimal(str(inv["taxable_value"])), cgst=Decimal(str(inv["cgst"])),
                sgst=Decimal(str(inv["sgst"])), igst=Decimal(str(inv["igst"])),
                total_value=Decimal(str(inv["total_value"])),
            ))
        self.db.commit()
        return upload

    def refresh(self):
        self.db.expire_all()

    def unread(self, user_key="associate", type_=None) -> list:
        q = self.db.query(models.Notification).filter(
            models.Notification.user_id == self.users[user_key].id)
        if type_:
            q = q.filter(models.Notification.type == type_)
        return q.all()

    def attempts(self) -> list:
        self.db.expire_all()
        return (self.db.query(models.GSTR2BFetchAttempt)
                .filter(models.GSTR2BFetchAttempt.client_gstin_id == self.gstin.id)
                .order_by(models.GSTR2BFetchAttempt.created_at.asc()).all())
