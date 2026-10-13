"""
GSTIN/PAN validation and purchase-register row validation.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.purchase_register import parse_purchase_register_file
from app.validation import (
    gstin_checksum, normalize_gstin, pan_from_gstin, period_bounds,
    state_from_gstin, validate_gstin, validate_invoice_date, validate_pan,
    validate_tax_split, looks_like_number,
)

# Real-world-format GSTINs whose check digits are genuinely correct.
VALID_GSTINS = ["27AAPFU0939F1ZV", "29AAGCB7383J1Z4"]


@pytest.mark.parametrize("gstin", VALID_GSTINS)
def test_valid_gstins_pass(gstin):
    ok, msg = validate_gstin(gstin)
    assert ok, msg


def test_checksum_is_computed_not_assumed():
    for gstin in VALID_GSTINS:
        assert gstin_checksum(gstin[:14]) == gstin[14]


def test_transposed_character_is_caught():
    """A regex-only check would accept this; the checksum does not."""
    good = "27AAPFU0939F1ZV"
    bad = "27AAPFU0939F1ZX"
    assert validate_gstin(good)[0] is True
    ok, msg = validate_gstin(bad)
    assert ok is False
    assert "check digit" in msg


@pytest.mark.parametrize("bad,fragment", [
    ("", "required"),
    ("27AAPFU0939F1Z", "15 characters"),
    ("27AAPFU0939F1ZVX", "15 characters"),
    ("ZZAAPFU0939F1ZV", "not a valid GSTIN format"),
    ("99AAPFU0939F1ZV", "check digit"),
])
def test_invalid_gstins_explain_themselves(bad, fragment):
    ok, msg = validate_gstin(bad)
    assert ok is False
    assert fragment.lower() in msg.lower()


def test_state_code_out_of_range_is_rejected():
    # State code 47 does not exist; build an otherwise well-formed GSTIN.
    stub = "47AAPFU0939F1Z"
    candidate = stub + gstin_checksum(stub)
    ok, msg = validate_gstin(candidate)
    assert ok is False
    assert "state code" in msg.lower()


def test_normalisation_strips_whitespace_and_case():
    assert normalize_gstin("  27aapfu0939f1zv ") == "27AAPFU0939F1ZV"
    assert normalize_gstin("27 AAPFU 0939 F1ZV") == "27AAPFU0939F1ZV"


def test_state_and_pan_extraction():
    code, name = state_from_gstin("27AAPFU0939F1ZV")
    assert code == "27" and name == "Maharashtra"
    assert pan_from_gstin("27AAPFU0939F1ZV") == "AAPFU0939F"


@pytest.mark.parametrize("pan,ok", [
    ("AAPFU0939F", True),
    ("AAPFU0939", False),
    ("AAPF10939F", False),
    ("aapfu0939f", True),
])
def test_pan_validation(pan, ok):
    assert validate_pan(pan)[0] is ok


def test_period_bounds():
    assert period_bounds("2026-03") == (date(2026, 3, 1), date(2026, 3, 31))
    assert period_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert period_bounds("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert period_bounds("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    assert period_bounds("nonsense") == (None, None)


def test_tax_split_rejects_both_intra_and_inter_state():
    ok, msg = validate_tax_split(Decimal("900"), Decimal("900"), Decimal("1800"))
    assert ok is False
    assert "intra-state or" in msg

    assert validate_tax_split(Decimal("900"), Decimal("900"), Decimal("0"))[0] is True
    assert validate_tax_split(Decimal("0"), Decimal("0"), Decimal("1800"))[0] is True


def test_future_invoice_date_is_an_error():
    ok, msg, sev = validate_invoice_date(date(2099, 1, 1), "2026-03")
    assert ok is False and sev == "error"


def test_out_of_period_date_is_only_a_warning():
    """A late-received invoice legitimately appears in a later return."""
    ok, msg, sev = validate_invoice_date(date(2026, 1, 15), "2026-03")
    assert ok is True and sev == "warning"
    assert "outside period" in msg


@pytest.mark.parametrize("value,ok", [
    ("1000", True), ("1000.50", True), ("", True),
    ("1,20,000", False), ("₹1000", False), ("(500)", False), ("abc", False),
])
def test_number_format_is_not_silently_repaired(value, ok):
    assert looks_like_number(value)[0] is ok


# ---------------------------------------------------------------------------
# Row-level parsing
# ---------------------------------------------------------------------------

HEADER = (b"vendor_name,vendor_gstin,invoice_number,invoice_date,"
          b"taxable_value,cgst,sgst,igst,total_value\n")


def test_clean_file_parses_without_errors():
    data = HEADER + b"Acme,27AAPFU0939F1ZV,INV-1,2026-03-05,10000,900,900,0,11800\n"
    out = parse_purchase_register_file(data, "pr.csv", period="2026-03")
    assert out.ok
    assert len(out.rows) == 1
    assert out.rows[0]["taxable_value"] == Decimal("10000.00")


def test_duplicate_invoice_is_rejected():
    data = (HEADER
            + b"Acme,27AAPFU0939F1ZV,INV-1,2026-03-05,10000,900,900,0,11800\n"
            + b"Acme,27AAPFU0939F1ZV,INV/1,2026-03-06,20000,1800,1800,0,23600\n")
    out = parse_purchase_register_file(data, "pr.csv", period="2026-03")
    assert not out.ok
    assert any("duplicate" in e.message.lower() for e in out.errors)


def test_total_is_derived_when_absent():
    data = (b"vendor_name,vendor_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst\n"
            b"Acme,27AAPFU0939F1ZV,INV-1,2026-03-05,10000,900,900,0\n")
    out = parse_purchase_register_file(data, "pr.csv", period="2026-03")
    assert out.ok
    assert out.rows[0]["total_value"] == Decimal("11800.00")


def test_missing_required_column_is_reported_once():
    data = b"vendor_name,invoice_number,invoice_date,taxable_value\nAcme,INV-1,2026-03-05,100\n"
    out = parse_purchase_register_file(data, "pr.csv", period="2026-03")
    assert not out.ok
    assert "vendor_gstin" in out.errors[0].message


def test_unsupported_file_type_is_rejected():
    out = parse_purchase_register_file(b"whatever", "notes.txt", period="2026-03")
    assert not out.ok
    assert "unsupported file type" in out.errors[0].message.lower()


def test_indian_date_format_is_read_day_first():
    """DD/MM/YYYY must not be read as MM/DD/YYYY."""
    data = HEADER + b"Acme,27AAPFU0939F1ZV,INV-1,05/03/2026,10000,900,900,0,11800\n"
    out = parse_purchase_register_file(data, "pr.csv", period="2026-03")
    assert out.ok
    assert out.rows[0]["invoice_date"] == date(2026, 3, 5)


def test_row_errors_carry_row_numbers_matching_the_spreadsheet():
    data = (HEADER
            + b"Acme,27AAPFU0939F1ZV,INV-1,2026-03-05,10000,900,900,0,11800\n"
            + b"Bad,27AAPFU0939F1ZX,INV-2,2026-03-06,10000,900,900,0,11800\n")
    out = parse_purchase_register_file(data, "pr.csv", period="2026-03")
    assert not out.ok
    # Header is row 1, first data row is row 2, so the bad row is row 3.
    assert out.errors[0].row == 3
