"""
GSTR-2B ingestion layer.

Today this is STUBBED — there is no live GSTN/GSP API integration. Instead:
  - `get_sample_gstr2b_doc()` loads the bundled sample GSTR-2B JSON for
    local testing (mirrors what a GSP API JSON response would look like).
  - `parse_gstr2b_json(contents)` parses an uploaded GSTR-2B JSON file into
    normalized row dicts, identical in shape to what the matching engine
    and DB layer expect.
  - `fetch_gstr2b(gstin, period)` is the swap-in point for a real GSP API
    call. It currently raises NotImplementedError with a clear message.
    When a real integration is added, only this function's body changes —
    callers (the upload router, the matching engine) don't need to change
    because they only ever see the same normalized row-dict shape.

Expected JSON shape (matches ClearTax/GSP-style GSTR-2B exports closely
enough for MVP purposes):
{
  "gstin": "...",
  "period": "2026-03",
  "generated_on": "2026-04-14",
  "invoices": [
    {
      "vendor_name": "...",          (optional)
      "vendor_gstin": "...",
      "invoice_number": "...",
      "invoice_date": "2026-03-05",
      "taxable_value": 12345.0,
      "cgst": 0.0, "sgst": 0.0, "igst": 0.0,
      "total_value": 12345.0,
      "filing_status": "FILED"        (optional, defaults to FILED)
    },
    ...
  ]
}
"""
import json
import os
from datetime import datetime
from typing import List, Tuple

from app.config import SAMPLE_DATA_DIR
from app.money import to_decimal

SAMPLE_GSTR2B_PATH = os.path.join(SAMPLE_DATA_DIR, "gstr2b_sample.json")


def get_sample_gstr2b_doc() -> dict:
    """Loads the bundled sample GSTR-2B document as-is (raw JSON dict)."""
    with open(SAMPLE_GSTR2B_PATH, encoding="utf-8") as f:
        return json.load(f)


def parse_gstr2b_json(contents: bytes) -> Tuple[List[dict], List[str], dict]:
    """Parses raw GSTR-2B JSON bytes into normalized row dicts.
    Returns (rows, errors, meta) where meta has gstin/period/generated_on."""
    errors: List[str] = []
    try:
        doc = json.loads(contents.decode("utf-8"))
    except Exception as e:
        return [], [f"Could not parse GSTR-2B JSON: {e}"], {}

    meta = {
        "gstin": doc.get("gstin"),
        "period": doc.get("period"),
        "generated_on": doc.get("generated_on"),
    }
    invoices = doc.get("invoices")
    if not isinstance(invoices, list):
        return [], ["GSTR-2B JSON is missing an 'invoices' array."], meta

    rows: List[dict] = []
    for i, inv in enumerate(invoices):
        try:
            vendor_gstin = str(inv["vendor_gstin"]).strip().upper()
            invoice_number = str(inv["invoice_number"]).strip()
            invoice_date = datetime.fromisoformat(str(inv["invoice_date"])).date()
            rows.append({
                "vendor_name": inv.get("vendor_name"),
                "vendor_gstin": vendor_gstin,
                "invoice_number": invoice_number,
                "invoice_date": invoice_date,
                "taxable_value": to_decimal(inv.get("taxable_value", 0)),
                "cgst": to_decimal(inv.get("cgst", 0)),
                "sgst": to_decimal(inv.get("sgst", 0)),
                "igst": to_decimal(inv.get("igst", 0)),
                "total_value": to_decimal(inv.get("total_value", 0)),
                "filing_status": inv.get("filing_status", "FILED"),
                # Live GSTN data says whether the credit is blocked (itcavl "N").
                # Hand-made files do not, and default to available.
                "itc_available": bool(inv.get("itc_available", True)),
            })
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"GSTR-2B invoice #{i + 1}: {e} — skipped.")

    return rows, errors, meta


def fetch_gstr2b(gstin: str, period: str) -> dict:
    """Swap-in point for a real GSP/GSTN API call.

    A production implementation would authenticate with a GSP (GST
    Suvidha Provider), call their GSTR-2B retrieval endpoint for
    (gstin, period), and return a dict shaped like the sample document
    above. The matching engine and DB layer never need to change —
    they only consume the normalized row dicts from parse_gstr2b_json.
    """
    raise NotImplementedError(
        "Live GSTN/GSP API integration is not wired up in this MVP. "
        "Use the 'load sample GSTR-2B' option or upload a GSTR-2B JSON file instead."
    )
