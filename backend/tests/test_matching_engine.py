"""
Unit tests for the matching engine, independent of the sample data files —
covers each branch of the priority order with small synthetic fixtures.

Run: pytest tests/test_matching_engine.py -v
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.matching.engine import (
    reconcile, as_entry, MATCHED, FORMATTING_MISMATCH,
    SUPPLIER_NOT_FILED, VALUE_MISMATCH, MISSING_IN_BOOKS,
)

GSTIN = "27AAPFU0939F1ZV"   # checksum-valid


def p(id, inv, d, taxable, vendor="Acme Pvt Ltd", gstin=GSTIN):
    return as_entry({
        "id": id, "vendor_name": vendor, "vendor_gstin": gstin,
        "invoice_number": inv, "invoice_date": d, "taxable_value": taxable,
        "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total_value": taxable,
    })


def g(id, inv, d, taxable, vendor="Acme Pvt Ltd", gstin=GSTIN, filing_status="FILED"):
    return as_entry({
        "id": id, "vendor_name": vendor, "vendor_gstin": gstin,
        "invoice_number": inv, "invoice_date": d, "taxable_value": taxable,
        "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "total_value": taxable,
        "filing_status": filing_status,
    })


def test_exact_match():
    purchases = [p("p1", "INV-001", date(2026, 3, 5), 10000.0)]
    gstr = [g("g1", "INV-001", date(2026, 3, 5), 10000.0)]
    results = reconcile(purchases, gstr)
    assert len(results) == 1
    assert results[0].match_status == MATCHED
    # Confidence is an integer percentage (0-100), not a 0-1 ratio.
    assert results[0].confidence_score >= 95
    assert isinstance(results[0].confidence_score, int)


def test_exact_match_normalizes_formatting():
    """Different separators/case that normalize identically, with value
    within the ₹1 tolerance, still count as an exact match."""
    purchases = [p("p1", "INV-2026-042", date(2026, 3, 5), 10000.00)]
    gstr = [g("g1", "inv/2026/042", date(2026, 3, 5), 10000.50)]
    results = reconcile(purchases, gstr)
    assert results[0].match_status == MATCHED


def test_formatting_mismatch():
    """Invoice number genuinely differs after normalization (leading zero
    dropped), but date/value are close -> formatting mismatch, auto-resolved."""
    purchases = [p("p1", "INV-2026-007", date(2026, 3, 5), 10000.0)]
    gstr = [g("g1", "INV-2026-7", date(2026, 3, 6), 10005.0)]
    results = reconcile(purchases, gstr)
    assert results[0].match_status == FORMATTING_MISMATCH
    assert 50 <= results[0].confidence_score <= 95


def test_value_mismatch_credit_note():
    purchases = [p("p1", "INV-050", date(2026, 3, 10), 200000.0)]
    gstr = [g("g1", "INV-050", date(2026, 3, 10), 150000.0)]
    results = reconcile(purchases, gstr)
    assert results[0].match_status == VALUE_MISMATCH
    assert "credit note" in results[0].notes.lower()


def test_supplier_not_filed():
    purchases = [p("p1", "INV-099", date(2026, 3, 15), 50000.0)]
    results = reconcile(purchases, [])
    assert results[0].match_status == SUPPLIER_NOT_FILED
    assert results[0].gstr2b_entry_id is None


def test_missing_in_books():
    gstr = [g("g1", "INV-777", date(2026, 3, 20), 30000.0)]
    results = reconcile([], gstr)
    assert results[0].match_status == MISSING_IN_BOOKS
    assert results[0].purchase_entry_id is None


def test_sequential_invoice_numbers_from_same_vendor_dont_cross_match():
    """Regression guard: two unrelated invoices from the same vendor whose
    numbers happen to look similar (e.g. INV-020 vs INV-032) must NOT be
    fuzzy-matched to each other."""
    purchases = [
        p("p1", "INV-2026-020", date(2026, 3, 3), 40000.0),
        p("p2", "INV-2026-032", date(2026, 3, 25), 60000.0),
    ]
    gstr = [g("g1", "INV-2026-020", date(2026, 3, 3), 40000.0)]
    results = reconcile(purchases, gstr)
    by_id = {r.purchase_entry_id: r for r in results}
    assert by_id["p1"].match_status == MATCHED
    assert by_id["p1"].gstr2b_entry_id == "g1"
    assert by_id["p2"].match_status == SUPPLIER_NOT_FILED


def test_one_to_one_matching_no_double_consumption():
    """A single GSTR-2B entry can only satisfy one purchase entry."""
    purchases = [
        p("p1", "INV-001", date(2026, 3, 5), 10000.0),
        p("p2", "INV-001-DUP", date(2026, 3, 5), 10000.0),
    ]
    gstr = [g("g1", "INV-001", date(2026, 3, 5), 10000.0)]
    results = reconcile(purchases, gstr)
    statuses = sorted(r.match_status for r in results)
    # One gets the exact match, the other must fall through unmatched.
    assert MATCHED in statuses
    assert statuses.count(MATCHED) == 1
