"""
Generates realistic sample data for local testing/demo:
  - backend/sample_data/purchase_register_sample.csv
  - backend/sample_data/gstr2b_sample.json

Distribution across ~36 purchase-register entries for period 2026-03:
  ~70% (25) match GSTR-2B exactly
  ~15% (5)  have formatting-only invoice number differences
  ~10% (4)  are missing from GSTR-2B entirely (supplier hasn't filed)
  ~5%  (2)  have value mismatches (simulated credit notes)
Plus 2 extra GSTR-2B-only entries to exercise the MISSING_IN_BOOKS case.

Run: python scripts/generate_sample_data.py
"""
import csv
import json
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.validation import gstin_checksum  # noqa: E402

random.seed(42)


def valid_gstin(stub: str) -> str:
    """Repair a hand-written GSTIN's check digit.

    The sample GSTINs below are invented, so their 15th character is
    arbitrary. Since the application now verifies the modulus-36 checksum,
    invented identifiers would be rejected on import. Recomputing the check
    digit keeps the sample data fictional but structurally valid.
    """
    stub = stub.strip().upper()
    return stub[:14] + gstin_checksum(stub[:14])

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "sample_data")
os.makedirs(OUT_DIR, exist_ok=True)

PERIOD = "2026-03"

VENDORS = [
    ("Shree Balaji Steel Traders", "27AAECS1234F1Z5"),
    ("Amrut Packaging Pvt Ltd", "24AABCA5678G1Z2"),
    ("Om Sai Electricals", "27AAEPO4567H1Z9"),
    ("Vishal Enterprises", "29AAJFV3456J1Z1"),
    ("Krishna Logistics", "06AAKCK7890K1Z4"),
    ("Suresh Hardware & Tools", "27AAOPS2345L1Z6"),
    ("National Paper Mills Ltd", "19AABCN6789M1Z3"),
    ("Ganesh Industrial Supplies", "27AAEPG8901N1Z7"),
    ("Rathi Metal Works", "08AAECR4321P1Z8"),
    ("Pooja Office Solutions", "27AABFP9012Q1Z0"),
    ("Sunrise Textiles Pvt Ltd", "24AACCS5432R1Z1"),
    ("Bharat Fasteners Co", "27AAJFB6754S1Z2"),
]

VENDORS = [(name, valid_gstin(g)) for name, g in VENDORS]
random.shuffle(VENDORS)

GST_RATE = 0.18  # combined CGST+SGST or IGST for these sample invoices


def taxable_and_tax(taxable):
    """Split GST as intra-state (CGST+SGST) 9%+9% for simplicity."""
    cgst = round(taxable * 0.09, 2)
    sgst = round(taxable * 0.09, 2)
    igst = 0.0
    total = round(taxable + cgst + sgst, 2)
    return cgst, sgst, igst, total


def rand_date():
    start = date(2026, 3, 1)
    return start + timedelta(days=random.randint(0, 27))


def make_invoice_number(idx):
    return f"INV-2026-{idx:03d}"


def formatted_variant(inv_no):
    """A variant that still LOOKS like the same invoice to a human, but
    normalizes to a *different* string than the original (e.g. supplier
    dropped a leading zero, or appended a revision suffix) — this is what
    genuinely triggers FORMATTING_MISMATCH rather than an exact match."""
    prefix, year, num = inv_no.split("-")  # e.g. INV, 2026, 042
    variants = [
        f"{prefix}/{year}/{int(num)}",          # leading zero dropped: INV/2026/42
        f"{prefix}-{year}-{num}A",               # revision suffix: INV-2026-042A
        f"{prefix.lower()}{year}{num}R",         # squashed + suffix: inv2026042R
        f"{prefix}/{year}/{num}-1",              # trailing -1
    ]
    return random.choice(variants)


purchase_rows = []
gstr2b_rows = []
pid = 1


def add_matched(idx):
    global pid
    vendor_name, gstin = VENDORS[idx % len(VENDORS)]
    inv_no = make_invoice_number(idx)
    d = rand_date()
    taxable = round(random.uniform(8000, 220000), 2)
    cgst, sgst, igst, total = taxable_and_tax(taxable)
    purchase_rows.append({
        "vendor_name": vendor_name, "vendor_gstin": gstin, "invoice_number": inv_no,
        "invoice_date": d.isoformat(), "taxable_value": taxable, "cgst": cgst,
        "sgst": sgst, "igst": igst, "total_value": total,
    })
    gstr2b_rows.append({
        "vendor_name": vendor_name, "vendor_gstin": gstin, "invoice_number": inv_no,
        "invoice_date": d.isoformat(), "taxable_value": taxable, "cgst": cgst,
        "sgst": sgst, "igst": igst, "total_value": total, "filing_status": "FILED",
    })
    pid += 1


def add_formatting_mismatch(idx):
    global pid
    vendor_name, gstin = VENDORS[idx % len(VENDORS)]
    inv_no = make_invoice_number(idx)
    d = rand_date()
    taxable = round(random.uniform(8000, 220000), 2)
    cgst, sgst, igst, total = taxable_and_tax(taxable)
    purchase_rows.append({
        "vendor_name": vendor_name, "vendor_gstin": gstin, "invoice_number": inv_no,
        "invoice_date": d.isoformat(), "taxable_value": taxable, "cgst": cgst,
        "sgst": sgst, "igst": igst, "total_value": total,
    })
    # GSTR-2B has the same invoice under a differently-formatted number,
    # a date within a couple of days, and a value within a few rupees
    # (rounding as the supplier reported it).
    gstr_inv_no = formatted_variant(inv_no)
    gstr_date = d + timedelta(days=random.choice([-2, -1, 0, 1, 2]))
    gstr_taxable = round(taxable + random.uniform(-8, 8), 2)
    g_cgst, g_sgst, g_igst, g_total = taxable_and_tax(gstr_taxable)
    gstr2b_rows.append({
        "vendor_name": vendor_name, "vendor_gstin": gstin, "invoice_number": gstr_inv_no,
        "invoice_date": gstr_date.isoformat(), "taxable_value": gstr_taxable, "cgst": g_cgst,
        "sgst": g_sgst, "igst": g_igst, "total_value": g_total, "filing_status": "FILED",
    })
    pid += 1


def add_supplier_not_filed(idx):
    global pid
    vendor_name, gstin = VENDORS[idx % len(VENDORS)]
    inv_no = make_invoice_number(idx)
    d = rand_date()
    taxable = round(random.uniform(8000, 220000), 2)
    cgst, sgst, igst, total = taxable_and_tax(taxable)
    purchase_rows.append({
        "vendor_name": vendor_name, "vendor_gstin": gstin, "invoice_number": inv_no,
        "invoice_date": d.isoformat(), "taxable_value": taxable, "cgst": cgst,
        "sgst": sgst, "igst": igst, "total_value": total,
    })
    # Intentionally no GSTR-2B row -> supplier hasn't filed GSTR-1 yet.
    pid += 1


def add_value_mismatch(idx):
    global pid
    vendor_name, gstin = VENDORS[idx % len(VENDORS)]
    inv_no = make_invoice_number(idx)
    d = rand_date()
    book_taxable = round(random.uniform(50000, 220000), 2)
    cgst, sgst, igst, total = taxable_and_tax(book_taxable)
    purchase_rows.append({
        "vendor_name": vendor_name, "vendor_gstin": gstin, "invoice_number": inv_no,
        "invoice_date": d.isoformat(), "taxable_value": book_taxable, "cgst": cgst,
        "sgst": sgst, "igst": igst, "total_value": total,
    })
    # Supplier issued a credit note the client hasn't recorded -> GSTR-2B
    # shows a materially lower taxable value against the SAME invoice number.
    credit_note_amount = round(book_taxable * random.uniform(0.15, 0.35), 2)
    gstr_taxable = round(book_taxable - credit_note_amount, 2)
    g_cgst, g_sgst, g_igst, g_total = taxable_and_tax(gstr_taxable)
    gstr2b_rows.append({
        "vendor_name": vendor_name, "vendor_gstin": gstin, "invoice_number": inv_no,
        "invoice_date": d.isoformat(), "taxable_value": gstr_taxable, "cgst": g_cgst,
        "sgst": g_sgst, "igst": g_igst, "total_value": g_total, "filing_status": "FILED",
    })
    pid += 1


def add_missing_in_books(idx):
    """A GSTR-2B entry with no counterpart in the purchase register at all
    -> client forgot to record the purchase."""
    vendor_name, gstin = VENDORS[idx % len(VENDORS)]
    inv_no = f"GHOST-{idx:03d}"
    d = rand_date()
    taxable = round(random.uniform(8000, 90000), 2)
    cgst, sgst, igst, total = taxable_and_tax(taxable)
    gstr2b_rows.append({
        "vendor_name": vendor_name, "vendor_gstin": gstin, "invoice_number": inv_no,
        "invoice_date": d.isoformat(), "taxable_value": taxable, "cgst": cgst,
        "sgst": sgst, "igst": igst, "total_value": total, "filing_status": "FILED",
    })


idx = 1
for _ in range(25):
    add_matched(idx); idx += 1
for _ in range(5):
    add_formatting_mismatch(idx); idx += 1
for _ in range(4):
    add_supplier_not_filed(idx); idx += 1
for _ in range(2):
    add_value_mismatch(idx); idx += 1
for _ in range(2):
    add_missing_in_books(idx); idx += 1

random.shuffle(purchase_rows)
random.shuffle(gstr2b_rows)

# --- write purchase register CSV ---
csv_path = os.path.join(OUT_DIR, "purchase_register_sample.csv")
fieldnames = ["vendor_name", "vendor_gstin", "invoice_number", "invoice_date",
              "taxable_value", "cgst", "sgst", "igst", "total_value"]
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(purchase_rows)

# --- write GSTR-2B JSON (stub for what a real GSP API response would look like) ---
json_path = os.path.join(OUT_DIR, "gstr2b_sample.json")
gstr2b_doc = {
    "gstin": valid_gstin("27AAAAA0000A1Z5"),
    "period": PERIOD,
    "generated_on": "2026-04-14",
    "invoices": gstr2b_rows,
}
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(gstr2b_doc, f, indent=2)

print(f"Wrote {len(purchase_rows)} purchase register rows -> {csv_path}")
print(f"Wrote {len(gstr2b_rows)} GSTR-2B rows -> {json_path}")
print(f"  matched={25} formatting={5} not_filed={4} value_mismatch={2} missing_in_books={2}")
