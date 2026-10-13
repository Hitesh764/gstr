"""
Contract tests for the Sandbox.co.in adapter.

Payloads are built from the shapes in Sandbox's published API reference and
served through httpx.MockTransport, so the adapter's real request building and
response parsing run end to end with no network. What is asserted is the wire
contract: paths, header names, the absence of a `Bearer` prefix, the way
failures arrive as HTTP 200, and how each documented error code is classified.

Where Sandbox's documentation is ambiguous the adapter marks the assumption
UNVERIFIED, and the tests that touch it say so.
"""
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from app.gsp.base import (
    ConsentExpiredError, GSPConfigurationError, GSPRateLimitedError,
    GSPResponseError, GSPUnavailableError, GSPValidationError, GSTINNotFoundError,
    InvalidOTPError, OTPRequestError, ReturnNotAvailableError,
)
from app.gsp.sandbox import SandboxAdapter

GSTIN = "27AAPFU0939F1ZV"
APP_TOKEN = "app-token-SECRET-1"
TAXPAYER_TOKEN = "taxpayer-token-SECRET-2"


def ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


T0 = datetime(2026, 4, 14, 6, 0, 0)
TOKEN_EXP = datetime(2026, 4, 14, 12, 0, 0)          # +6 hours
SESSION_EXP = datetime(2026, 5, 14, 6, 0, 0)         # +30 days


class Recorder:
    """Records every request and answers from a routing table."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.routes: dict = {}

    def route(self, method: str, path: str, response):
        self.routes[(method, path)] = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        answer = self.routes.get(key)
        if answer is None:
            return httpx.Response(404, json={"code": 404, "message": f"no route {key}"})
        return answer(request) if callable(answer) else answer

    def calls(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def ok(data: dict, txn: str = "txn-1") -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "data": data, "timestamp": 1, "transaction_id": txn})


def failure(error_cd: str, message: str = "boom", txn: str = "txn-err") -> httpx.Response:
    """Sandbox reports most failures as HTTP 200 with status_cd "0"."""
    return ok({"status_cd": "0", "error": {"error_cd": error_cd, "message": message}}, txn)


@pytest.fixture
def rec():
    r = Recorder()
    r.route("POST", "/authenticate", ok({"access_token": APP_TOKEN}))
    return r


def make(rec, **kw) -> SandboxAdapter:
    defaults = dict(base_url="https://api.sandbox.test", api_key="key_live_x",
                    api_secret="secret_live_y", client=httpx.Client(transport=httpx.MockTransport(rec)))
    defaults.update(kw)
    return SandboxAdapter(**defaults)


# ---------------------------------------------------------------------------
# Application token
# ---------------------------------------------------------------------------

def test_authenticate_sends_key_and_secret_headers(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp", ok({"status_cd": "1"}))
    make(rec).initiate_otp_consent(GSTIN, "acme.user")

    auth = rec.calls("/authenticate")[0]
    assert auth.headers["x-api-key"] == "key_live_x"
    assert auth.headers["x-api-secret"] == "secret_live_y"
    assert auth.headers["x-api-version"] == "1.0.0"


def test_token_is_sent_without_a_bearer_prefix(rec):
    """Sandbox states its token 'is NOT a bearer token'."""
    rec.route("POST", "/gst/compliance/tax-payer/otp", ok({"status_cd": "1"}))
    make(rec).initiate_otp_consent(GSTIN, "acme.user")

    sent = rec.calls("/gst/compliance/tax-payer/otp")[0].headers["authorization"]
    assert sent == APP_TOKEN
    assert not sent.lower().startswith("bearer")
    assert rec.calls("/gst/compliance/tax-payer/otp")[0].headers["x-api-key"] == "key_live_x"


def test_application_token_is_cached_across_calls(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp", ok({"status_cd": "1"}))
    adapter = make(rec)
    adapter.initiate_otp_consent(GSTIN, "u")
    adapter.initiate_otp_consent(GSTIN, "u")
    assert len(rec.calls("/authenticate")) == 1


def test_application_token_is_renewed_before_it_can_expire(rec):
    """Sandbox's token lasts 24h; the adapter renews at 23h."""
    rec.route("POST", "/gst/compliance/tax-payer/otp", ok({"status_cd": "1"}))
    clock = {"t": 0.0}
    adapter = make(rec, monotonic=lambda: clock["t"])

    adapter.initiate_otp_consent(GSTIN, "u")
    clock["t"] = 22 * 3600
    adapter.initiate_otp_consent(GSTIN, "u")
    assert len(rec.calls("/authenticate")) == 1

    clock["t"] = 23 * 3600 + 1
    adapter.initiate_otp_consent(GSTIN, "u")
    assert len(rec.calls("/authenticate")) == 2


def test_missing_credentials_fail_before_any_request(rec):
    adapter = make(rec, api_key="", api_secret="")
    with pytest.raises(GSPConfigurationError) as exc:
        adapter.initiate_otp_consent(GSTIN, "u")
    assert "SANDBOX_API_KEY" in str(exc.value)
    assert rec.requests == []


def test_rejected_credentials_are_a_configuration_error_not_a_consent_error(rec):
    rec.route("POST", "/authenticate", httpx.Response(401, json={"message": "no"}))
    with pytest.raises(GSPConfigurationError) as exc:
        make(rec).initiate_otp_consent(GSTIN, "u")
    assert "Contact support" in str(exc.value)
    assert not exc.value.retryable


# ---------------------------------------------------------------------------
# OTP consent
# ---------------------------------------------------------------------------

def test_otp_request_wire_format(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp", ok({"status_cd": "1"}, "txn-otp"))
    result = make(rec).initiate_otp_consent(GSTIN, "acme.user")

    req = rec.calls("/gst/compliance/tax-payer/otp")[0]
    assert json.loads(req.content) == {"username": "acme.user", "gstin": GSTIN}
    assert req.headers["x-source"] == "primary"
    assert req.headers["content-type"] == "application/json"
    assert result.transaction_id == "txn-otp"
    assert "acme.user" in result.message


def test_otp_request_maximum_sessions_is_a_clear_error(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp", failure("AUTH403"))
    with pytest.raises(OTPRequestError) as exc:
        make(rec).initiate_otp_consent(GSTIN, "u")
    assert "maximum number of active sessions" in str(exc.value)
    assert exc.value.provider_code == "AUTH403"


def test_otp_server_down_is_retryable(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp", failure("TEC4001", "OTP server is down"))
    with pytest.raises(GSPUnavailableError) as exc:
        make(rec).initiate_otp_consent(GSTIN, "u")
    assert exc.value.retryable is True


def test_invalid_gstin_pattern_is_a_validation_error(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp",
              httpx.Response(422, json={"code": 422, "message": "Invalid GSTIN pattern"}))
    with pytest.raises(GSPValidationError) as exc:
        make(rec).initiate_otp_consent("BAD", "u")
    assert "Invalid GSTIN pattern" in str(exc.value)
    assert not exc.value.retryable


def verify_ok(token_exp=TOKEN_EXP, session_exp=SESSION_EXP):
    data = {"access_token": TAXPAYER_TOKEN, "status_cd": "1"}
    if token_exp:
        data["token_expiry"] = ms(token_exp)
    if session_exp:
        data["session_expiry"] = ms(session_exp)
    return ok(data, "txn-verify")


def test_otp_verify_wire_format_and_two_distinct_expiries(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp/verify", verify_ok())
    grant = make(rec).verify_otp_consent(GSTIN, "acme.user", "575757")

    req = rec.calls("/gst/compliance/tax-payer/otp/verify")[0]
    assert req.url.params["otp"] == "575757"
    assert json.loads(req.content) == {"username": "acme.user", "gstin": GSTIN}
    assert req.headers["authorization"] == APP_TOKEN, "verify is authorised by the application token"

    assert grant.access_token == TAXPAYER_TOKEN
    assert grant.token_expires_at == TOKEN_EXP
    assert grant.session_expires_at == SESSION_EXP, "the consent window comes from the response"
    assert grant.session_expires_at > grant.token_expires_at


def test_the_access_token_never_appears_in_repr(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp/verify", verify_ok())
    grant = make(rec).verify_otp_consent(GSTIN, "u", "1")
    assert TAXPAYER_TOKEN not in repr(grant)
    assert TAXPAYER_TOKEN not in str(grant)


def test_wrong_otp_arrives_as_http_200_and_is_still_caught(rec):
    """The trap: a status-code check would call this a success."""
    rec.route("POST", "/gst/compliance/tax-payer/otp/verify", failure("AUTH4033", "Invalid Session"))
    with pytest.raises(InvalidOTPError):
        make(rec).verify_otp_consent(GSTIN, "u", "000000")


def test_verify_refuses_a_response_that_states_no_expiry(rec):
    """The consent window must come from the provider. It is never invented."""
    rec.route("POST", "/gst/compliance/tax-payer/otp/verify", verify_ok(None, None))
    with pytest.raises(GSPResponseError) as exc:
        make(rec).verify_otp_consent(GSTIN, "u", "1")
    assert "expires" in str(exc.value)


def test_a_single_reported_expiry_is_used_conservatively_for_both(rec):
    """If only one expiry is given, treating the session as ending with the
    token can understate consent but can never overstate it."""
    rec.route("POST", "/gst/compliance/tax-payer/otp/verify", verify_ok(TOKEN_EXP, None))
    grant = make(rec).verify_otp_consent(GSTIN, "u", "1")
    assert grant.token_expires_at == grant.session_expires_at == TOKEN_EXP


def test_missing_access_token_is_an_error(rec):
    rec.route("POST", "/gst/compliance/tax-payer/otp/verify",
              ok({"status_cd": "1", "token_expiry": ms(TOKEN_EXP)}))
    with pytest.raises(GSPResponseError):
        make(rec).verify_otp_consent(GSTIN, "u", "1")


# ---------------------------------------------------------------------------
# Session refresh
# ---------------------------------------------------------------------------

def test_refresh_uses_the_taxpayer_token_and_extends_the_grant(rec):
    new_exp = datetime(2026, 4, 14, 18, 0, 0)
    rec.route("POST", "/gst/compliance/tax-payer/session/refresh",
              ok({"access_token": "refreshed-token", "status_cd": "1",
                  "token_expiry": ms(new_exp), "session_expiry": ms(SESSION_EXP)}))
    grant = make(rec).refresh_consent(TAXPAYER_TOKEN)

    req = rec.calls("/gst/compliance/tax-payer/session/refresh")[0]
    assert req.headers["authorization"] == TAXPAYER_TOKEN
    assert grant.token_expires_at == new_exp
    assert grant.session_expires_at == SESSION_EXP
    assert grant.access_token == "refreshed-token"


@pytest.mark.parametrize("response", [
    httpx.Response(401, json={"message": "expired"}),
    failure("AUTH4033", "Invalid Session"),
])
def test_refresh_after_the_session_ends_means_consent_expired(rec, response):
    rec.route("POST", "/gst/compliance/tax-payer/session/refresh", response)
    with pytest.raises(ConsentExpiredError) as exc:
        make(rec).refresh_consent(TAXPAYER_TOKEN)
    assert exc.value.retryable is False


# ---------------------------------------------------------------------------
# GSTR-2B
# ---------------------------------------------------------------------------

def b2b_invoice(**over):
    inv = {"inum": "INV-001", "dt": "05-03-2026", "val": 118000.0, "txval": 100000.0,
           "igst": 0.0, "cgst": 9000.0, "sgst": 9000.0, "cess": 0.0, "itcavl": "Y",
           "typ": "R", "rev": "N", "pos": "27", "rsn": "", "imsStatus": "N"}
    inv.update(over)
    return inv


def gstr2b_body(*, gstin=GSTIN, rtnprd="032026", b2b=None, cdnr=None, status_cd="1", extra=None):
    """The documented triple envelope: Sandbox data -> GSTN wrapper -> payload."""
    inner = {
        "cpsumm": {}, "docdata": {"b2b": b2b if b2b is not None else [], "cdnr": cdnr or []},
        "gendt": "14-04-2026", "gstin": gstin, "rtnprd": rtnprd, "version": "1.0",
    }
    data = {"status_cd": status_cd,
            "data": {"chksum": "abc", "data": inner, "timestamp": 1, "transaction_id": "gstn-1"}}
    data.update(extra or {})
    return ok(data, "txn-2b")


SUPPLIER = {"ctin": "29AAGCB7383J1Z4", "trdnm": "Bharat Steel Pvt Ltd",
            "supfildt": "11-04-2026", "supprd": "032026", "inv": [b2b_invoice()]}


@pytest.mark.parametrize("period,basis,expected", [
    ("2024-11", "financial", "/gst/compliance/tax-payer/gstrs/gstr-2b/2024/112024"),
    # January to March belongs to the financial year that began the previous April.
    ("2025-02", "financial", "/gst/compliance/tax-payer/gstrs/gstr-2b/2024/022025"),
    ("2025-04", "financial", "/gst/compliance/tax-payer/gstrs/gstr-2b/2025/042025"),
    # UNVERIFIED against a live account: the switch exists because the docs
    # describe the year as "financial" while GSTR-1 uses the calendar year.
    ("2025-02", "calendar", "/gst/compliance/tax-payer/gstrs/gstr-2b/2025/022025"),
])
def test_gstr2b_path_year_and_month_format(rec, period, basis, expected):
    rec.route("GET", expected, gstr2b_body(rtnprd=f"{period[5:]}{period[:4]}", b2b=[SUPPLIER]))
    make(rec, gstr2b_year_basis=basis).fetch_gstr2b(GSTIN, period, TAXPAYER_TOKEN)
    req = rec.calls(expected)[0]
    assert req.headers["authorization"] == TAXPAYER_TOKEN
    assert req.headers["x-api-key"] == "key_live_x"


PATH_MAR_2026 = "/gst/compliance/tax-payer/gstrs/gstr-2b/2025/032026"


def test_gstr2b_invoices_are_normalised(rec):
    supplier = {**SUPPLIER, "inv": [
        b2b_invoice(),
        b2b_invoice(inum="INV-002", dt="09-03-2026", val=59000.0, txval=50000.0,
                    cgst=0.0, sgst=0.0, igst=9000.0, itcavl="N", rev="Y"),
    ]}
    rec.route("GET", PATH_MAR_2026, gstr2b_body(b2b=[supplier]))
    result = make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)

    assert result.invoice_count == 2 and result.pages == 1
    first, second = result.document["invoices"]
    assert first == {
        "vendor_name": "Bharat Steel Pvt Ltd", "vendor_gstin": "29AAGCB7383J1Z4",
        "invoice_number": "INV-001", "invoice_date": "2026-03-05",
        "taxable_value": 100000.0, "cgst": 9000.0, "sgst": 9000.0, "igst": 0.0,
        "total_value": 118000.0, "itc_available": True, "filing_status": "FILED",
        "invoice_type": "R", "reverse_charge": False, "supplier_filed_on": "2026-04-11",
    }
    assert second["itc_available"] is False, "itcavl 'N' means the credit is blocked"
    assert second["reverse_charge"] is True
    assert second["igst"] == 9000.0
    assert result.document["gstin"] == GSTIN
    assert result.document["period"] == "2026-03"
    assert result.document["generated_on"] == "2026-04-14"


def test_normalised_document_is_accepted_by_the_existing_parser(rec):
    """Live data must take exactly the path a manual upload takes."""
    from app.ingestion.gstr2b import parse_gstr2b_json

    rec.route("GET", PATH_MAR_2026, gstr2b_body(b2b=[SUPPLIER]))
    result = make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
    rows, errors, meta = parse_gstr2b_json(json.dumps(result.document).encode())

    assert errors == [] and len(rows) == 1
    assert rows[0]["invoice_number"] == "INV-001"
    assert rows[0]["itc_available"] is True
    assert meta["gstin"] == GSTIN


def test_credit_notes_are_counted_and_kept_apart_from_invoices(rec):
    cdnr = [{"ctin": "29AAGCB7383J1Z4", "trdnm": "Bharat Steel", "nt": [
        {"ntnum": "CN-9", "typ": "C", "dt": "20-03-2026", "val": 5900.0, "txval": 5000.0,
         "igst": 0.0, "cgst": 450.0, "sgst": 450.0, "itcavl": "Y"}]}]
    rec.route("GET", PATH_MAR_2026, gstr2b_body(b2b=[SUPPLIER], cdnr=cdnr))
    result = make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)

    assert result.invoice_count == 1, "notes must not be counted as invoices"
    assert result.credit_note_count == 1
    note = result.document["credit_notes"][0]
    assert note["note_number"] == "CN-9" and note["note_type"] == "C"
    assert note["taxable_value"] == 5000.0


def test_tax_is_summed_from_line_items_when_not_on_the_invoice(rec):
    inv = {"inum": "L-1", "dt": "05-03-2026", "val": 118.0, "itcavl": "Y",
           "itms": [{"num": 1, "itm_det": {"txval": 60, "iamt": 0, "camt": 5.4, "samt": 5.4}},
                    {"num": 2, "itm_det": {"txval": 40, "iamt": 0, "camt": 3.6, "samt": 3.6}}]}
    rec.route("GET", PATH_MAR_2026, gstr2b_body(b2b=[{**SUPPLIER, "inv": [inv]}]))
    got = make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN).document["invoices"][0]
    assert got["taxable_value"] == 100.0 and got["cgst"] == 9.0 and got["sgst"] == 9.0


def test_unreadable_invoices_are_skipped_with_a_warning_not_dropped_silently(rec):
    broken = b2b_invoice(inum="", dt="not-a-date")
    rec.route("GET", PATH_MAR_2026, gstr2b_body(b2b=[{**SUPPLIER, "inv": [b2b_invoice(), broken]}]))
    result = make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
    assert result.invoice_count == 1
    assert len(result.warnings) == 1 and "Skipped an invoice" in result.warnings[0]


def test_paginated_statement_fetches_every_file_and_merges(rec):
    """status_cd 3 plus a file count means fetch file_number 1..fc."""
    page1 = gstr2b_body(b2b=[SUPPLIER])
    page2 = gstr2b_body(b2b=[{**SUPPLIER, "ctin": "07AAACH1925Q1ZC",
                              "inv": [b2b_invoice(inum="INV-P2")]}])

    def answer(request: httpx.Request):
        n = request.url.params.get("file_number")
        if n is None:
            return ok({"status_cd": "3", "fc": 2})
        return {"1": page1, "2": page2}[n]

    rec.route("GET", PATH_MAR_2026, answer)
    result = make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)

    assert result.pages == 2 and result.invoice_count == 2
    assert {i["invoice_number"] for i in result.document["invoices"]} == {"INV-001", "INV-P2"}
    pages_asked = sorted(r.url.params["file_number"] for r in rec.requests
                         if "file_number" in r.url.params)
    assert pages_asked == ["1", "2"]


def test_an_absurd_page_count_is_refused_rather_than_looped(rec):
    rec.route("GET", PATH_MAR_2026, ok({"status_cd": "3", "fc": 10_000_000}))
    with pytest.raises(GSPResponseError):
        make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)


def test_gstr2b_not_yet_generated_is_retryable(rec):
    """Normal in the early hours of the 14th: it resolves by itself."""
    rec.route("GET", PATH_MAR_2026, failure("RET2B1023", "GSTR-2B not available"))
    with pytest.raises(ReturnNotAvailableError) as exc:
        make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
    assert exc.value.retryable is True


def test_no_details_available_is_an_empty_statement_not_a_failure(rec):
    rec.route("GET", PATH_MAR_2026, failure("RET2B1016", "No details available to view"))
    result = make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
    assert result.invoice_count == 0 and result.document["invoices"] == []
    assert any("no GSTR-2B details" in w for w in result.warnings)


def test_a_statement_for_a_different_gstin_is_refused(rec):
    rec.route("GET", PATH_MAR_2026, gstr2b_body(gstin="29AAGCB7383J1Z4", b2b=[SUPPLIER]))
    with pytest.raises(GSPResponseError) as exc:
        make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
    assert "29AAGCB7383J1Z4" in str(exc.value)


def test_a_statement_for_a_different_period_is_refused(rec):
    rec.route("GET", PATH_MAR_2026, gstr2b_body(rtnprd="022026", b2b=[SUPPLIER]))
    with pytest.raises(GSPResponseError) as exc:
        make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
    assert "2026-02" in str(exc.value)


def test_a_response_without_docdata_is_a_bad_response(rec):
    rec.route("GET", PATH_MAR_2026, ok({"status_cd": "1", "data": {"unexpected": True}}))
    with pytest.raises(GSPResponseError):
        make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)


# ---------------------------------------------------------------------------
# Failure classification: the scheduler relies on `retryable`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer,error,retryable", [
    (httpx.Response(401, json={}), ConsentExpiredError, False),
    (httpx.Response(403, json={}), ConsentExpiredError, False),
    (failure("AUTH4033"), ConsentExpiredError, False),
    (httpx.Response(500, json={}), GSPUnavailableError, True),
    (httpx.Response(503, json={}), GSPUnavailableError, True),
    (httpx.Response(429, json={}, headers={"retry-after": "120"}), GSPRateLimitedError, True),
    (failure("TEC9999", "portal fault"), GSPUnavailableError, True),
    (failure("XYZ123", "something odd"), GSPResponseError, False),
    (httpx.Response(422, json={"message": "bad month"}), GSPValidationError, False),
])
def test_taxpayer_call_failures_are_classified(rec, answer, error, retryable):
    rec.route("GET", PATH_MAR_2026, answer)
    with pytest.raises(error) as exc:
        make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
    assert exc.value.retryable is retryable


def test_rate_limit_carries_the_retry_after_hint(rec):
    rec.route("GET", PATH_MAR_2026, httpx.Response(429, json={}, headers={"retry-after": "120"}))
    with pytest.raises(GSPRateLimitedError) as exc:
        make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
    assert exc.value.retry_after == 120


def test_timeout_and_connection_failure_are_retryable(rec):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    for boom in (timeout, refused):
        rec.route("GET", PATH_MAR_2026, boom)
        with pytest.raises(GSPUnavailableError) as exc:
            make(rec).fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)
        assert exc.value.retryable is True


def test_a_stale_application_token_is_replaced_once_transparently(rec):
    """If Sandbox rejects the cached application token, re-authenticate once."""
    tokens = iter(["stale-app-token", "fresh-app-token"])
    rec.route("POST", "/authenticate", lambda r: ok({"access_token": next(tokens)}))
    seen = []

    def otp(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401, json={}) if len(seen) == 1 else ok({"status_cd": "1"})

    rec.route("POST", "/gst/compliance/tax-payer/otp", otp)
    make(rec).initiate_otp_consent(GSTIN, "u")
    assert seen == ["stale-app-token", "fresh-app-token"]


# ---------------------------------------------------------------------------
# GSTIN verification and GSTR-1
# ---------------------------------------------------------------------------

def test_verify_gstin_parses_the_public_register_entry(rec):
    rec.route("POST", "/gst/compliance/public/gstin/search", ok({
        "status_cd": "1",
        "data": {"gstin": "29AFSPB9500E1ZY", "lgnm": "Vicky Pvt Ltd", "tradeNam": "Vicky Trading",
                 "sts": "Active", "ctb": "Proprietorship",
                 "pradr": {"addr": {"stcd": "Karnataka"}}}}))
    info = make(rec).verify_gstin("29AFSPB9500E1ZY")
    assert (info.legal_name, info.trade_name) == ("Vicky Pvt Ltd", "Vicky Trading")
    assert info.state_name == "Karnataka" and info.is_active is True


def test_a_cancelled_registration_is_reported_not_raised(rec):
    rec.route("POST", "/gst/compliance/public/gstin/search",
              ok({"status_cd": "1", "data": {"gstin": GSTIN, "lgnm": "X", "sts": "Cancelled"}}))
    info = make(rec).verify_gstin(GSTIN)
    assert info.is_active is False and info.status == "Cancelled"


def test_unknown_gstin(rec):
    rec.route("POST", "/gst/compliance/public/gstin/search", failure("FO8000", "No records found"))
    with pytest.raises(GSTINNotFoundError):
        make(rec).verify_gstin(GSTIN)


def test_fetch_gstr1_b2b(rec):
    body = ok({"status_cd": "1", "data": {"b2b": [{"ctin": "07ABCCD6123D1YZ", "inv": [{
        "inum": "78618", "idt": "29-12-2023", "val": 4718.82,
        "itms": [{"num": 1, "itm_det": {"rt": 18, "txval": 3999, "iamt": 719.82,
                                        "camt": 0, "samt": 0, "csamt": 0}}]}]}]}})
    rec.route("GET", "/gst/compliance/tax-payer/gstrs/gstr-1/b2b/2023/12", body)
    result = make(rec).fetch_gstr1(GSTIN, "2023-12", TAXPAYER_TOKEN)

    assert len(result.invoices) == 1
    inv = result.invoices[0]
    assert inv["customer_gstin"] == "07ABCCD6123D1YZ" and inv["invoice_date"] == "2023-12-29"
    assert inv["taxable_value"] == 3999.0 and inv["igst"] == 719.82


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_no_token_secret_or_otp_is_ever_logged(rec, caplog):
    caplog.set_level(logging.DEBUG)
    rec.route("POST", "/gst/compliance/tax-payer/otp", ok({"status_cd": "1"}))
    rec.route("POST", "/gst/compliance/tax-payer/otp/verify", verify_ok())
    rec.route("GET", PATH_MAR_2026, gstr2b_body(b2b=[SUPPLIER]))

    adapter = make(rec)
    adapter.initiate_otp_consent(GSTIN, "acme.user")
    adapter.verify_otp_consent(GSTIN, "acme.user", "987654")
    adapter.fetch_gstr2b(GSTIN, "2026-03", TAXPAYER_TOKEN)

    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (APP_TOKEN, TAXPAYER_TOKEN, "secret_live_y", "key_live_x", "987654"):
        assert secret not in text, f"{secret!r} leaked into the logs"


def test_otp_redaction_hides_the_code_but_keeps_the_request_readable(rec, caplog):
    """The redaction must not merely delete the log line or mangle it: the
    request should still be visible, with only the code blanked."""
    caplog.set_level(logging.INFO)
    rec.route("POST", "/gst/compliance/tax-payer/otp/verify", verify_ok())
    make(rec).verify_otp_consent(GSTIN, "u", "987654")

    lines = [r.getMessage() for r in caplog.records if "otp/verify" in r.getMessage()]
    assert lines, "the request should still be logged"
    assert any("/otp/verify?otp=***" in line for line in lines), lines
    assert not any("987654" in line for line in lines)


@pytest.mark.parametrize("raw,expected", [
    ("GET https://x/y?otp=123456 done", "GET https://x/y?otp=*** done"),
    ("GET https://x/y?a=1&otp=123456&b=2", "GET https://x/y?a=1&otp=***&b=2"),
    ("GET https://x/y?OTP=123456", "GET https://x/y?OTP=***"),
    ("GET https://x/y?note=otpless", "GET https://x/y?note=otpless"),   # untouched
])
def test_redaction_pattern(raw, expected):
    from app.gsp.sandbox import _RedactOTPFilter
    record = logging.LogRecord("httpx", logging.INFO, "f", 1, raw, (), None)
    _RedactOTPFilter().filter(record)
    assert record.getMessage() == expected
