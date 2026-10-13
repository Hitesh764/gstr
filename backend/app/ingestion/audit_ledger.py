"""Strict CSV/XLSX parser for open debtor and creditor ledger items."""
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd

from app.ingestion.purchase_register import RowIssue


@dataclass
class LedgerParseOutcome:
    rows: list[dict] = field(default_factory=list)
    errors: list[RowIssue] = field(default_factory=list)


def _normalize(value: str) -> str:
    return re.sub(r"[\s-]+", "_", str(value).strip().lower()).strip("_")


def _date(value: str) -> date:
    # ISO dates avoid locale-dependent day/month interpretation in audit data.
    return date.fromisoformat(str(value).strip())


def _amount(value: str) -> Decimal:
    raw = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", raw):
        raise InvalidOperation
    amount = Decimal(raw)
    if amount >= Decimal("10000000000000000"):
        raise InvalidOperation
    return amount.quantize(Decimal("0.01"))


def parse_audit_ledger(contents: bytes, filename: str) -> LedgerParseOutcome:
    result = LedgerParseOutcome()
    try:
        if filename.lower().endswith(".csv"):
            frame = pd.read_csv(io.BytesIO(contents), dtype=str, keep_default_na=False)
        elif filename.lower().endswith(".xlsx"):
            frame = pd.read_excel(io.BytesIO(contents), dtype=str, keep_default_na=False)
        else:
            raise ValueError("Unsupported file type. Upload a .csv or .xlsx file.")
    except Exception as exc:
        result.errors.append(RowIssue(str(exc), severity="error"))
        return result
    if frame.empty:
        result.errors.append(RowIssue("The file contains no data rows."))
        return result
    frame.columns = [_normalize(column) for column in frame.columns]
    aliases = {
        "type": "account_type", "ledger_type": "account_type",
        "customer_vendor": "party_name", "counterparty": "party_name",
        "vendor_name": "party_name", "customer_name": "party_name",
        "invoice_no": "invoice_number", "voucher_no": "invoice_number",
        "document_number": "invoice_number", "document_date": "invoice_date",
        "bill_date": "invoice_date", "outstanding": "open_balance",
        "balance": "open_balance", "narration": "explanation", "notes": "explanation",
        "ledger_code": "account_code", "ledger_name": "account_name",
    }
    frame = frame.rename(columns={column: aliases[column] for column in frame.columns if column in aliases})
    required = ["account_type", "party_name", "invoice_number", "invoice_date", "open_balance"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        result.errors.append(RowIssue(
            "Missing required column(s): " + ", ".join(missing)
            + ". Expected account_type, party_name, invoice_number, invoice_date, open_balance; due_date and explanation are optional."
        ))
        return result
    for optional in ("due_date", "explanation", "account_code", "account_name"):
        if optional not in frame.columns:
            frame[optional] = ""

    for index, raw in frame.iterrows():
        rownum = int(index) + 2
        get = lambda key: str(raw.get(key, "") or "").strip()
        account_type = get("account_type").upper().replace(" ", "_")
        if account_type in {"DEBTOR", "RECEIVABLE", "RECEIVABLES"}:
            account_type = "DEBTORS"
        if account_type in {"CREDITOR", "PAYABLE", "PAYABLES"}:
            account_type = "CREDITORS"
        errors = []
        if account_type not in {"DEBTORS", "CREDITORS"}:
            errors.append(RowIssue("account_type must be DEBTORS or CREDITORS.", rownum, "account_type"))
        for required_field in ("party_name", "invoice_number", "invoice_date", "open_balance"):
            if not get(required_field):
                errors.append(RowIssue(f"{required_field} is required.", rownum, required_field))
        try:
            invoice_date = _date(get("invoice_date"))
        except ValueError:
            invoice_date = None
            errors.append(RowIssue("invoice_date must use YYYY-MM-DD format.", rownum, "invoice_date"))
        due_raw = get("due_date")
        try:
            due_date = _date(due_raw) if due_raw else None
        except ValueError:
            due_date = None
            errors.append(RowIssue("due_date must use YYYY-MM-DD format.", rownum, "due_date"))
        try:
            balance = _amount(get("open_balance"))
        except (InvalidOperation, ValueError):
            balance = Decimal("0.00")
            errors.append(RowIssue("open_balance must be a non-negative plain decimal with at most two places.", rownum, "open_balance"))
        if errors:
            result.errors.extend(errors)
            continue
        result.rows.append({
            "account_type": account_type,
            "account_code": get("account_code") or None,
            "account_name": get("account_name") or None,
            "party_name": get("party_name"),
            "invoice_number": get("invoice_number"),
            "invoice_date": invoice_date,
            "due_date": due_date,
            "open_balance": balance,
            "explanation": get("explanation") or None,
            "source_row_number": rownum,
        })
    if not result.rows and not result.errors:
        result.errors.append(RowIssue("The file contains no ledger items."))
    return result
