"""
Purchase register ingestion: parses an uploaded CSV/XLSX into normalized
row dicts, with per-row validation reported before anything is imported.

Expected columns (case-insensitive, spaces/underscores interchangeable):
  vendor_name, vendor_gstin, invoice_number, invoice_date,
  taxable_value, cgst, sgst, igst, total_value

Validation philosophy
---------------------
Errors reject the row; warnings import it but flag it. The split matters
because a CA needs to distinguish "this file is wrong" from "this looks
unusual, confirm it". Specifically:

  error   — invalid GSTIN checksum, unparseable or future date, currency
            symbols in numeric fields, both CGST/SGST and IGST populated,
            duplicate invoice within the same file
  warning — invoice dated outside the stated period (legitimate for a
            late-received invoice), total that does not equal the sum of
            its parts

Nothing is silently repaired. A '₹1,23,456' cell is rejected rather than
stripped, because silently accepting it hides a broken export that will
keep producing broken data every month.
"""
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, List, Optional, Tuple

import pandas as pd

from app.money import money_str, to_decimal, ZERO
from app.matching.normalize import normalize_invoice_number
from app.validation import (
    looks_like_number, normalize_gstin, validate_gstin, validate_invoice_date,
    validate_tax_split,
)

REQUIRED_COLUMNS = [
    "vendor_name", "vendor_gstin", "invoice_number", "invoice_date", "taxable_value",
]
OPTIONAL_NUMERIC_COLUMNS = ["cgst", "sgst", "igst", "total_value"]

# A total more than one rupee away from taxable + taxes is worth flagging.
TOTAL_MISMATCH_TOLERANCE = Decimal("1.00")


@dataclass
class RowIssue:
    message: str
    row: Optional[int] = None
    field: Optional[str] = None
    severity: str = "error"     # "error" | "warning"

    def as_dict(self) -> dict:
        return {"row": self.row, "field": self.field,
                "message": self.message, "severity": self.severity}


@dataclass
class ParseOutcome:
    rows: List[dict] = field(default_factory=list)
    errors: List[RowIssue] = field(default_factory=list)
    warnings: List[RowIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error_dicts(self) -> list[dict]:
        return [e.as_dict() for e in self.errors]

    def warning_dicts(self) -> list[dict]:
        return [w.as_dict() for w in self.warnings]


def _normalize_col(col: str) -> str:
    return re.sub(r"[\s_]+", "_", str(col).strip().lower())


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}")


def _parse_date(raw: str):
    """Parse a date cell, resolving the DD/MM vs MM/DD ambiguity correctly.

    An ISO string (YYYY-MM-DD) is unambiguous and parsed as-is. Anything
    else is read day-first, because Indian accounting exports write
    DD/MM/YYYY and pandas would otherwise default to the US month-first
    reading. Getting this backwards silently shifts invoice dates, which
    then fail the ±3-day formatting-match window for no visible reason.
    """
    raw = (raw or "").strip()
    if _ISO_DATE_RE.match(raw):
        return pd.to_datetime(raw, errors="coerce")
    return pd.to_datetime(raw, errors="coerce", dayfirst=True)


def parse_purchase_register_file(
    contents: bytes,
    filename: str,
    period: Optional[str] = None,
) -> ParseOutcome:
    """Parse and validate an uploaded purchase register.

    `period` enables the date-range check; omit it to skip that check.
    """
    outcome = ParseOutcome()
    lower_name = (filename or "").lower()

    try:
        if lower_name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(contents), dtype=str, keep_default_na=False)
        elif lower_name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(contents), dtype=str)
        else:
            outcome.errors.append(RowIssue(
                f"Unsupported file type for '{filename}'. Upload a .csv or .xlsx file."
            ))
            return outcome
    except Exception as exc:
        outcome.errors.append(RowIssue(f"Could not read '{filename}': {exc}"))
        return outcome

    if df.empty:
        outcome.errors.append(RowIssue("The file contains no data rows."))
        return outcome

    df.columns = [_normalize_col(c) for c in df.columns]

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        outcome.errors.append(RowIssue(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Expected: {', '.join(REQUIRED_COLUMNS + OPTIONAL_NUMERIC_COLUMNS)}."
        ))
        return outcome

    for col in OPTIONAL_NUMERIC_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    seen_invoices: dict[tuple, int] = {}

    for idx, raw in df.iterrows():
        excel_row = int(idx) + 2   # +1 for zero-index, +1 for the header row
        row_errors: List[RowIssue] = []
        row_warnings: List[RowIssue] = []

        def err(msg: str, fld: Optional[str] = None):
            row_errors.append(RowIssue(msg, row=excel_row, field=fld, severity="error"))

        def warn(msg: str, fld: Optional[str] = None):
            row_warnings.append(RowIssue(msg, row=excel_row, field=fld, severity="warning"))

        def cell(name: str) -> str:
            v = raw.get(name, "")
            if v is None:
                return ""
            s = str(v).strip()
            return "" if s.lower() in ("nan", "none") else s

        # -- required text fields ------------------------------------------
        vendor_name = cell("vendor_name")
        vendor_gstin = normalize_gstin(cell("vendor_gstin"))
        invoice_number = cell("invoice_number")

        if not vendor_name:
            err("vendor_name is empty.", "vendor_name")
        if not invoice_number:
            err("invoice_number is empty.", "invoice_number")

        if not vendor_gstin:
            err("vendor_gstin is empty.", "vendor_gstin")
        else:
            ok, msg = validate_gstin(vendor_gstin)
            if not ok:
                err(msg, "vendor_gstin")

        # -- invoice date ---------------------------------------------------
        invoice_date: Optional[date] = None
        raw_date = cell("invoice_date")
        if not raw_date:
            err("invoice_date is empty.", "invoice_date")
        else:
            parsed = _parse_date(raw_date)
            if pd.isna(parsed):
                err(f"invoice_date '{raw_date}' could not be read as a date.", "invoice_date")
            else:
                invoice_date = parsed.date()
                ok, msg, severity = validate_invoice_date(invoice_date, period or "")
                if msg and severity == "error":
                    err(msg, "invoice_date")
                elif msg and severity == "warning":
                    warn(msg, "invoice_date")

        # -- money ----------------------------------------------------------
        amounts: dict[str, Decimal] = {}
        for col in ["taxable_value", "cgst", "sgst", "igst", "total_value"]:
            raw_val = cell(col)
            ok, msg = looks_like_number(raw_val)
            if not ok:
                err(f"{col}: {msg}", col)
                amounts[col] = ZERO
                continue
            amounts[col] = to_decimal(raw_val) if raw_val else ZERO

        if amounts.get("taxable_value", ZERO) <= ZERO and not row_errors:
            err("taxable_value must be greater than zero.", "taxable_value")

        # -- logical consistency ---------------------------------------------
        ok, msg = validate_tax_split(
            amounts.get("cgst", ZERO), amounts.get("sgst", ZERO), amounts.get("igst", ZERO)
        )
        if not ok:
            err(msg, "igst")

        total = amounts.get("total_value", ZERO)
        computed = (amounts.get("taxable_value", ZERO) + amounts.get("cgst", ZERO)
                    + amounts.get("sgst", ZERO) + amounts.get("igst", ZERO))
        if total == ZERO and computed > ZERO:
            total = computed
        elif total > ZERO and abs(total - computed) > TOTAL_MISMATCH_TOLERANCE:
            warn(
                f"total_value ₹{money_str(total)} does not equal taxable + taxes "
                f"(₹{money_str(computed)}), a difference of "
                f"₹{money_str(abs(total - computed))}.",
                "total_value",
            )

        # -- duplicate detection within this file -----------------------------
        if vendor_gstin and invoice_number:
            key = (vendor_gstin, normalize_invoice_number(invoice_number))
            if key in seen_invoices:
                err(
                    f"Duplicate invoice: {invoice_number} from {vendor_gstin} already "
                    f"appears at row {seen_invoices[key]}. Each invoice should appear once.",
                    "invoice_number",
                )
            else:
                seen_invoices[key] = excel_row

        outcome.errors.extend(row_errors)
        outcome.warnings.extend(row_warnings)

        if row_errors:
            continue   # row rejected

        outcome.rows.append({
            "vendor_name": vendor_name,
            "vendor_gstin": vendor_gstin,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "taxable_value": amounts["taxable_value"],
            "cgst": amounts["cgst"],
            "sgst": amounts["sgst"],
            "igst": amounts["igst"],
            "total_value": total,
            "source_row_number": excel_row,
        })

    return outcome
