"""
A GSP that needs no credentials.

Lets the whole flow (connect, OTP, refresh, scheduled fetch, expiry) run
locally and in tests without a Sandbox account. It is refused outside
development by the factory in app/gsp/__init__.py, because fake filings served
to a real client would be worse than a refusal to start.

It is deliberately STATELESS. The access token encodes when its session ends,
so a request handled by one process can be refreshed or checked by another,
which is how the real flow behaves across web requests and background workers.

Try it:
    OTP for every GSTIN ............ 123456   (anything else is "incorrect")
    username "no-such-user" ........ the OTP request is refused
    session length ................. MOCK_GSP_SESSION_HOURS (default 168 = 7 days)
"""
import base64
import json
import secrets
from datetime import date, datetime, timedelta

from app.gsp.base import (
    ConsentExpiredError, ConsentGrant, GSPProvider, GSTINInfo, GSTR1Result,
    GSTR2BResult, InvalidOTPError, OTPRequestError, OTPRequestResult,
)

VALID_OTP = "123456"
TOKEN_LIFETIME = timedelta(hours=6)     # mirrors a real taxpayer token


def _now() -> datetime:
    return datetime.utcnow()


def _encode(gstin: str, session_end: datetime) -> str:
    payload = {"g": gstin, "s": int(session_end.timestamp()), "n": secrets.token_hex(4)}
    return "mock." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode(token: str) -> dict:
    try:
        if not token.startswith("mock."):
            raise ValueError("not a mock token")
        return json.loads(base64.urlsafe_b64decode(token[5:].encode()))
    except Exception as exc:
        raise ConsentExpiredError("That access token is not valid.") from exc


class MockGSPProvider(GSPProvider):
    name = "mock"
    supports_refresh = True

    def __init__(self, session_hours: int = 168):
        self._session_hours = session_hours

    def verify_gstin(self, gstin: str) -> GSTINInfo:
        from app.validation import state_from_gstin
        _, state = state_from_gstin(gstin)
        pan = gstin[2:12]
        return GSTINInfo(
            gstin=gstin.upper(), legal_name=f"Demo Traders {pan} Pvt Ltd",
            trade_name=f"Demo Traders {pan}", status="Active",
            state_name=state, constitution="Private Limited Company",
        )

    def initiate_otp_consent(self, gstin: str, username: str) -> OTPRequestResult:
        if username.strip().lower() == "no-such-user":
            raise OTPRequestError("That GST portal username was not recognised.")
        return OTPRequestResult(
            message="[MOCK] No real OTP was sent. Enter 123456.",
            transaction_id="mock-otp-request",
        )

    def verify_otp_consent(self, gstin: str, username: str, otp: str) -> ConsentGrant:
        if otp.strip() != VALID_OTP:
            raise InvalidOTPError("That OTP is incorrect or has expired.")
        now = _now()
        session_end = now + timedelta(hours=self._session_hours)
        return ConsentGrant(
            access_token=_encode(gstin, session_end),
            token_expires_at=min(now + TOKEN_LIFETIME, session_end),
            session_expires_at=session_end,
            transaction_id="mock-otp-verify",
        )

    def refresh_consent(self, access_token: str) -> ConsentGrant:
        claims = _decode(access_token)
        session_end = datetime.utcfromtimestamp(claims["s"])
        now = _now()
        if now >= session_end:
            raise ConsentExpiredError("The consent window has ended. A new OTP is needed.")
        return ConsentGrant(
            access_token=_encode(claims["g"], session_end),
            token_expires_at=min(now + TOKEN_LIFETIME, session_end),
            session_expires_at=session_end,
            transaction_id="mock-refresh",
        )

    def _require_live(self, access_token: str) -> dict:
        claims = _decode(access_token)
        if _now() >= datetime.utcfromtimestamp(claims["s"]):
            raise ConsentExpiredError("The consent window has ended. A new OTP is needed.")
        return claims

    def fetch_gstr2b(self, gstin: str, period: str, access_token: str) -> GSTR2BResult:
        self._require_live(access_token)
        from app.ingestion.gstr2b import get_sample_gstr2b_doc

        sample = get_sample_gstr2b_doc()
        year, month = (int(x) for x in period.split("-"))
        invoices = []
        for inv in sample["invoices"]:
            original = date.fromisoformat(inv["invoice_date"])
            shifted = date(year, month, min(original.day, 28))   # keep dates inside the period
            invoices.append({**inv, "invoice_date": shifted.isoformat(), "itc_available": True})

        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        document = {
            "gstin": gstin.upper(), "period": period,
            "generated_on": date(next_year, next_month, 14).isoformat(),
            "invoices": invoices,
            "credit_notes": [{
                "vendor_gstin": invoices[0]["vendor_gstin"] if invoices else "",
                "vendor_name": invoices[0].get("vendor_name") if invoices else None,
                "note_number": "CN-MOCK-001", "note_type": "C",
                "note_date": f"{year:04d}-{month:02d}-20", "taxable_value": 5000.0,
                "cgst": 450.0, "sgst": 450.0, "igst": 0.0, "total_value": 5900.0,
                "itc_available": True,
            }],
        }
        return GSTR2BResult(
            document=document, raw={"pages": [], "mock": True},
            invoice_count=len(invoices), credit_note_count=1, pages=1,
            transaction_id="mock-gstr2b",
        )

    def fetch_gstr1(self, gstin: str, period: str, access_token: str) -> GSTR1Result:
        self._require_live(access_token)
        return GSTR1Result(
            gstin=gstin.upper(), period=period,
            invoices=[{"customer_gstin": "29AAGCB7383J1Z4", "invoice_number": "S-001",
                       "invoice_date": f"{period}-05", "taxable_value": 100000.0,
                       "cgst": 0.0, "sgst": 0.0, "igst": 18000.0, "total_value": 118000.0}],
            raw={"mock": True}, transaction_id="mock-gstr1",
        )
