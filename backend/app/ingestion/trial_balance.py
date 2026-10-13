"""Strict CSV/XLSX parsing for normalized trial-balance snapshots."""
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional

import pandas as pd

from app.ingestion.purchase_register import RowIssue


@dataclass
class TrialBalanceParseOutcome:
    rows: list[dict] = field(default_factory=list)
    errors: list[RowIssue] = field(default_factory=list)
    warnings: list[RowIssue] = field(default_factory=list)


def _normalize_col(value: str) -> str:
    return re.sub(r"[\s_-]+", "_", str(value).strip().lower()).strip("_")


def _read_frame(contents: bytes, filename: str):
    lower = (filename or "").lower()
    if lower.endswith(".csv"):
        return pd.read_csv(io.BytesIO(contents), dtype=str, keep_default_na=False)
    if lower.endswith(".xlsx"):
        return pd.read_excel(io.BytesIO(contents), dtype=str, keep_default_na=False)
    raise ValueError("Unsupported file type. Upload a .csv or .xlsx file.")


def _parse_amount(raw: str) -> Decimal:
    value = str(raw).strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,2})?", value):
        raise InvalidOperation
    amount = Decimal(value)
    if amount.copy_abs() >= Decimal("10000000000000000"):
        raise InvalidOperation
    return amount.quantize(Decimal("0.01"))


def parse_trial_balance_file(contents: bytes, filename: str) -> TrialBalanceParseOutcome:
    """Parse a TB snapshot with `account_code`, `account_name`, and `amount`.

    `account_group` is optional and retained as supplied for later account
    mapping. Amounts must be plain decimal values; currency symbols,
    thousands separators, formulas and implicit repairs are rejected.
    """
    result = TrialBalanceParseOutcome()
    try:
        frame = _read_frame(contents, filename)
    except Exception as exc:
        result.errors.append(RowIssue(str(exc), severity="error"))
        return result
    if frame.empty:
        result.errors.append(RowIssue("The file contains no data rows."))
        return result

    frame.columns = [_normalize_col(column) for column in frame.columns]
    # Common, unambiguous labels used by accounting exports.
    aliases = {
        "ledger_code": "account_code",
        "account": "account_name",
        "ledger_name": "account_name",
        "group": "account_group",
        "closing_balance": "amount",
        "balance": "amount",
        "trial_balance_amount": "amount",
    }
    frame = frame.rename(columns={c: aliases[c] for c in frame.columns if c in aliases})
    required = ["account_code", "account_name", "amount"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        result.errors.append(RowIssue(
            f"Missing required column(s): {', '.join(missing)}. Expected account_code, account_name, amount, and optionally account_group."
        ))
        return result
    if "account_group" not in frame.columns:
        frame["account_group"] = ""

    seen: dict[str, int] = {}
    for index, raw in frame.iterrows():
        row_number = int(index) + 2

        def cell(column: str) -> str:
            value = raw.get(column, "")
            if value is None or pd.isna(value):
                return ""
            text = str(value).strip()
            return "" if text.lower() in {"nan", "none"} else text

        account_code = cell("account_code")
        account_name = cell("account_name")
        account_group = cell("account_group") or None
        amount_text = cell("amount")
        if not account_code and not account_name and not amount_text and not account_group:
            continue  # ignore wholly blank spreadsheet rows

        row_errors: list[RowIssue] = []
        if not account_code:
            row_errors.append(RowIssue("account_code is empty.", row_number, "account_code"))
        if not account_name:
            row_errors.append(RowIssue("account_name is empty.", row_number, "account_name"))
        if not amount_text:
            row_errors.append(RowIssue("amount is empty.", row_number, "amount"))
            amount = Decimal("0.00")
        else:
            try:
                amount = _parse_amount(amount_text)
            except (InvalidOperation, ValueError):
                row_errors.append(RowIssue(
                    f"amount '{amount_text}' is not a plain decimal with at most two places; remove currency symbols or separators.",
                    row_number, "amount",
                ))
                amount = Decimal("0.00")

        key = account_code.casefold()
        if account_code and key in seen:
            row_errors.append(RowIssue(
                f"Duplicate account_code '{account_code}' already appears at row {seen[key]}.",
                row_number, "account_code",
            ))
        elif account_code:
            seen[key] = row_number

        result.errors.extend(row_errors)
        if not row_errors:
            result.rows.append({
                "account_code": account_code,
                "account_name": account_name,
                "account_group": account_group,
                "amount": amount,
                "source_row_number": row_number,
            })

    if not result.rows and not result.errors:
        result.errors.append(RowIssue("The file contains no account rows."))
    return result
