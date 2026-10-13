"""
Standalone proof that the matching engine works correctly against the
sample data — no database, no HTTP server involved.

Run: python scripts/test_matching.py
"""
import csv
import json
import os
import sys
from collections import Counter
from datetime import date

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.matching.engine import reconcile, as_entry, MATCHED, FORMATTING_MISMATCH, \
    SUPPLIER_NOT_FILED, VALUE_MISMATCH, MISSING_IN_BOOKS
from app.config import SAMPLE_DATA_DIR


def load_purchase_register(csv_path):
    entries = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            entries.append(as_entry({
                "id": f"p{i}",
                "vendor_name": row["vendor_name"],
                "vendor_gstin": row["vendor_gstin"],
                "invoice_number": row["invoice_number"],
                "invoice_date": date.fromisoformat(row["invoice_date"]),
                "taxable_value": float(row["taxable_value"]),
                "cgst": float(row["cgst"]),
                "sgst": float(row["sgst"]),
                "igst": float(row["igst"]),
                "total_value": float(row["total_value"]),
            }))
    return entries


def load_gstr2b(json_path):
    with open(json_path, encoding="utf-8") as f:
        doc = json.load(f)
    entries = []
    for i, row in enumerate(doc["invoices"]):
        entries.append(as_entry({
            "id": f"g{i}",
            "vendor_name": row.get("vendor_name"),
            "vendor_gstin": row["vendor_gstin"],
            "invoice_number": row["invoice_number"],
            "invoice_date": date.fromisoformat(row["invoice_date"]),
            "taxable_value": float(row["taxable_value"]),
            "cgst": float(row["cgst"]),
            "sgst": float(row["sgst"]),
            "igst": float(row["igst"]),
            "total_value": float(row["total_value"]),
            "filing_status": row.get("filing_status", "FILED"),
        }))
    return entries


def main():
    purchase_csv = os.path.join(SAMPLE_DATA_DIR, "purchase_register_sample.csv")
    gstr2b_json = os.path.join(SAMPLE_DATA_DIR, "gstr2b_sample.json")

    purchase_entries = load_purchase_register(purchase_csv)
    gstr2b_entries = load_gstr2b(gstr2b_json)

    print(f"Loaded {len(purchase_entries)} purchase register entries")
    print(f"Loaded {len(gstr2b_entries)} GSTR-2B entries")
    print()

    results = reconcile(purchase_entries, gstr2b_entries)

    counts = Counter(r.match_status for r in results)
    total_purchase_itc = sum(p.taxable_value for p in purchase_entries)
    total_gstr2b_itc = sum(g.taxable_value for g in gstr2b_entries)
    itc_at_risk = sum(
        (r.purchase_entry_id and next(p for p in purchase_entries if p.id == r.purchase_entry_id).taxable_value or 0)
        for r in results if r.match_status in (SUPPLIER_NOT_FILED, VALUE_MISMATCH)
    )

    print("=== Match status breakdown ===")
    for status in [MATCHED, FORMATTING_MISMATCH, VALUE_MISMATCH, SUPPLIER_NOT_FILED, MISSING_IN_BOOKS]:
        print(f"  {status:22s}: {counts.get(status, 0)}")
    print(f"  {'TOTAL results':22s}: {len(results)}")
    print()

    print("=== ITC summary ===")
    print(f"  Total ITC in books:          Rs {total_purchase_itc:,.2f}")
    print(f"  Total ITC eligible (2B):     Rs {total_gstr2b_itc:,.2f}")
    print(f"  Total ITC at risk:           Rs {itc_at_risk:,.2f}")
    auto_matched_pct = 100.0 * counts.get(MATCHED, 0) / max(len(purchase_entries), 1)
    print(f"  % auto-matched (exact):      {auto_matched_pct:.1f}%")
    print()

    print("=== Sample exception explanations ===")
    shown = 0
    for r in results:
        if r.match_status != MATCHED and shown < 6:
            print(f"  [{r.match_status}] (confidence={r.confidence_score}) {r.notes}")
            shown += 1
    print()

    # --- sanity assertions proving the engine matches the spec'd distribution ---
    assert counts.get(MATCHED, 0) == 25, f"expected 25 exact matches, got {counts.get(MATCHED, 0)}"
    assert counts.get(FORMATTING_MISMATCH, 0) == 5, f"expected 5 formatting mismatches, got {counts.get(FORMATTING_MISMATCH, 0)}"
    assert counts.get(SUPPLIER_NOT_FILED, 0) == 4, f"expected 4 supplier-not-filed, got {counts.get(SUPPLIER_NOT_FILED, 0)}"
    assert counts.get(VALUE_MISMATCH, 0) == 2, f"expected 2 value mismatches, got {counts.get(VALUE_MISMATCH, 0)}"
    assert counts.get(MISSING_IN_BOOKS, 0) == 2, f"expected 2 missing-in-books, got {counts.get(MISSING_IN_BOOKS, 0)}"
    assert len(results) == len(purchase_entries) + 2, "one result per purchase entry, plus the 2 missing-in-books"

    print("ALL ASSERTIONS PASSED — matching engine behaves exactly as specified.")


if __name__ == "__main__":
    main()
