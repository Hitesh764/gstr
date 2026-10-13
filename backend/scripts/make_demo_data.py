"""
Builds the ready-to-upload demo files in `demo_data/` at the project root.

Three files are produced:

  purchase_register_2026-03.csv     clean, 36 invoices, every GSTIN valid
  gstr2b_2026-03.json               the matching GSTR-2B statement
  purchase_register_with_errors.csv deliberately broken, to see validation work

The clean pair reproduces the distribution the matching engine is designed
around: about 70% match exactly, 15% differ only in invoice-number
formatting, 10% are missing from GSTR-2B because the supplier has not filed,
and 5% carry a value mismatch standing in for an unrecorded credit note.

The broken file exercises one validation rule per row, so the upload screen
shows a representative error for each: bad GSTIN checksum, missing vendor,
CGST/SGST together with IGST, a currency symbol, a thousands separator, a
future date, a duplicate invoice, and a total that does not add up.

Run: python scripts/make_demo_data.py
"""
import csv
import json
import os
import shutil
import sys
from datetime import date, timedelta

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from app.config import SAMPLE_DATA_DIR  # noqa: E402
from app.validation import gstin_checksum, validate_gstin  # noqa: E402

PROJECT_DIR = os.path.dirname(BACKEND_DIR)
OUT_DIR = os.path.join(PROJECT_DIR, "demo_data")
PERIOD = "2026-03"

FIELDS = ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date",
          "taxable_value", "cgst", "sgst", "igst", "total_value"]


def valid_gstin(stub: str) -> str:
    """Repair an invented GSTIN's check digit so it passes validation."""
    stub = stub.strip().upper()
    return stub[:14] + gstin_checksum(stub[:14])


def build_error_file(path: str) -> list[str]:
    """One broken row per validation rule, plus one clean row for contrast."""
    good_a = valid_gstin("27AABCO5678D1Z0")
    good_b = valid_gstin("29AAGCP7890G1Z0")
    future = (date.today() + timedelta(days=400)).isoformat()

    rows = [
        # Clean row, so the file is not uniformly bad.
        ["Valid Vendor Pvt Ltd", good_a, "INV-2026-501", "2026-03-04",
         "125000", "11250", "11250", "0", "147500"],
        # Invalid checksum: last character deliberately wrong.
        ["Wrong Checksum Traders", good_b[:14] + ("A" if good_b[14] != "A" else "B"),
         "INV-2026-502", "2026-03-05", "84000", "7560", "7560", "0", "99120"],
        # Missing vendor name.
        ["", good_a, "INV-2026-503", "2026-03-06", "42000", "3780", "3780", "0", "49560"],
        # Both intra-state and inter-state tax on one invoice.
        ["Both Taxes Ltd", good_b, "INV-2026-504", "2026-03-07",
         "96000", "8640", "8640", "17280", "130560"],
        # Currency symbol in a numeric field.
        ["Currency Symbol Co", good_a, "INV-2026-505", "2026-03-09",
         "₹68000", "6120", "6120", "0", "80240"],
        # Thousands separators in Indian grouping.
        ["Grouped Number Co", good_b, "INV-2026-506", "2026-03-10",
         "1,20,000", "0", "0", "21600", "141600"],
        # Date in the future.
        ["Future Dated Ltd", good_a, "INV-2026-507", future,
         "55000", "4950", "4950", "0", "64900"],
        # Duplicate of the first invoice, differently formatted.
        ["Valid Vendor Pvt Ltd", good_a, "INV/2026/501", "2026-03-04",
         "125000", "11250", "11250", "0", "147500"],
        # Total that does not equal taxable plus taxes: a warning, not an error.
        ["Odd Total Enterprises", good_b, "INV-2026-508", "2026-03-12",
         "73000", "0", "0", "13140", "999999"],
        # Invoice from an earlier period: also a warning, since a late-received
        # invoice may legitimately be claimed in a later return.
        ["Late Received Traders", good_a, "INV-2026-509", "2026-01-22",
         "31000", "2790", "2790", "0", "36580"],
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        w.writerows(rows)
    return rows


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    src_csv = os.path.join(SAMPLE_DATA_DIR, "purchase_register_sample.csv")
    src_json = os.path.join(SAMPLE_DATA_DIR, "gstr2b_sample.json")
    if not (os.path.exists(src_csv) and os.path.exists(src_json)):
        raise SystemExit(
            "Sample data is missing. Run `python scripts/generate_sample_data.py` first."
        )

    clean_csv = os.path.join(OUT_DIR, f"purchase_register_{PERIOD}.csv")
    clean_json = os.path.join(OUT_DIR, f"gstr2b_{PERIOD}.json")
    error_csv = os.path.join(OUT_DIR, "purchase_register_with_errors.csv")

    shutil.copyfile(src_csv, clean_csv)
    shutil.copyfile(src_json, clean_json)
    build_error_file(error_csv)

    # Verify what we just wrote, rather than assuming it.
    with open(clean_csv, encoding="utf-8") as f:
        clean_rows = list(csv.DictReader(f))
    bad = [r["vendor_gstin"] for r in clean_rows
           if not validate_gstin(r["vendor_gstin"])[0]]
    if bad:
        raise SystemExit(f"Clean demo file contains invalid GSTINs: {bad}")

    doc = json.load(open(clean_json, encoding="utf-8"))
    client_gstin = doc["gstin"]

    readme = os.path.join(OUT_DIR, "README.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(f"""# Demo data

Ready-to-upload files for testing the reconciliation flow.

| File | What it is |
|---|---|
| `purchase_register_{PERIOD}.csv` | {len(clean_rows)} purchase invoices, all valid |
| `gstr2b_{PERIOD}.json` | The matching GSTR-2B statement |
| `purchase_register_with_errors.csv` | Deliberately broken, to see validation reporting |

## Important

`gstr2b_{PERIOD}.json` is issued for GSTIN **{client_gstin}**, which is the
seeded demo client. Uploading it against a different GSTIN is rejected on
purpose: a GSTR-2B belonging to another registration would reconcile against
nothing and look like a mass filing failure, so the mismatch is caught at
upload instead.

If you want to use these files with your own client, either add a client
with GSTIN `{client_gstin}`, or edit the `gstin` field at the top of the JSON
to match yours.

## Using them

1. Sign in as `demo@sharma-ca.in`.
2. Open the client, choose a GSTIN, and go to Upload data.
3. Set the period to `{PERIOD}`.
4. Drop `purchase_register_{PERIOD}.csv` into the first box.
5. Drop `gstr2b_{PERIOD}.json` into the second, or press Load sample GSTR-2B.
6. Run reconciliation.

Expect roughly 25 exact matches, 5 formatting-only differences, 4 invoices
the supplier has not filed, 2 value mismatches, and 2 invoices present in
GSTR-2B but missing from the books.

## Seeing validation work

Upload `purchase_register_with_errors.csv` instead. Nothing is imported and
each problem is reported against its spreadsheet row number: invalid GSTIN
checksum, empty vendor name, CGST/SGST combined with IGST, a currency
symbol, thousands separators, a future date, and a duplicate invoice. Two
rows import with warnings rather than errors, because a total that does not
add up and an invoice dated outside the period are both worth flagging but
neither is necessarily wrong.

## If your own test file has invalid GSTINs

Invented GSTINs almost never satisfy the modulus-36 check digit. To repair a
file rather than hand-editing it:

```bash
cd backend
python scripts/fix_gstin_checksums.py path/to/your.csv
```

That writes a corrected copy alongside the original and reports every change.
""")

    print(f"Wrote demo data to {OUT_DIR}")
    print(f"  purchase_register_{PERIOD}.csv        {len(clean_rows)} rows, 0 invalid GSTINs")
    print(f"  gstr2b_{PERIOD}.json                  {len(doc['invoices'])} invoices, "
          f"issued for {client_gstin}")
    print(f"  purchase_register_with_errors.csv  10 rows exercising the validation rules")
    print(f"  README.md")


if __name__ == "__main__":
    main()
