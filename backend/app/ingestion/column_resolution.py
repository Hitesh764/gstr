"""Hidden, deterministic sales-register header resolution and normalization.

Only this import boundary knows external column labels. Rules are versioned in
source and persisted client profiles can safely remember resolved headers.
"""
from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from difflib import SequenceMatcher
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import pandas as pd
from app.validation import STATE_NAMES

ALIAS_VERSION = "gstr1-sales-aliases-v5"

# Alias keys and values are normalized using normalize_header before matching.
ALIASES: dict[str, tuple[str, ...]] = {
    "customer_name": ("customer name", "customer", "party", "party name", "buyer", "buyer name", "client", "client name", "recipient name", "account name", "ledger name", "name of recipient", "party ledger name", "customer billing name", "billing customer name"),
    "customer_gstin": ("gstin", "gstin no", "gst number", "gst no", "gst registration number", "customer gstin", "customer gst no", "customer gst number", "party gstin", "party gst no", "buyer gstin", "recipient gstin", "gst registration no", "gst number of recipient", "tax registration number", "customer billing gstin", "customer bill to gstin", "bill to gstin"),
    "invoice_number": ("invoice number", "invoice no", "invoice num", "invoice", "inv number", "inv no", "inv num", "bill number", "bill no", "voucher number", "voucher no", "document number", "document no", "doc number", "doc no", "reference number", "reference no", "voucher ref", "transaction number", "sales number"),
    "invoice_date": ("invoice date", "bill date", "invoice bill date", "document date", "doc date", "inv date", "voucher date", "transaction date", "date of invoice", "sales date", "billing date"),
    "taxable_value": ("taxable value", "taxable amount", "taxable amt", "taxable", "assessable value", "assessable amount", "net taxable value", "taxable turnover", "taxable base", "value before tax", "sum of taxable value", "sum of taxable amount"),
    "cgst": ("cgst", "cgst amount", "cgst amt", "central tax", "central tax amount", "central tax amt", "sum of cgst amount", "sum of central tax amount"),
    "sgst": ("sgst", "sgst amount", "sgst amt", "state tax", "state tax amount", "state tax amt", "utgst", "utgst amount", "sum of sgst amount", "sum of state tax amount"),
    "igst": ("igst", "igst amount", "igst amt", "integrated tax", "integrated tax amount", "integrated tax amt", "sum of igst amount", "sum of integrated tax amount"),
    "cess": ("cess", "cess amount", "cess amt"),
    "invoice_value": ("invoice value", "invoice amount", "total amount", "total value", "bill amount", "bill value", "grand total", "net invoice value", "gross amount", "total invoice value", "sum of invoice value", "sum of invoice amount"),
    # Billing, shipping, customer and destination states are distinct concepts;
    # only explicit supply/POS headings resolve directly to this field.
    "place_of_supply": ("place of supply", "pos", "supply state", "place of supply state", "state of supply", "destination state", "gst pos", "pos state"),
    "hsn_sac": ("hsn", "hsn code", "hsn sac", "sac", "sac code", "hsn sac code", "hsn slash sac"),
    "gst_rate": ("gst rate", "gst percent", "gst percentage", "tax rate", "tax percent", "tax percentage", "gst rate percent", "rate of tax", "rate percent"),
    "document_type": ("document type", "voucher type", "invoice type", "transaction type", "document kind", "voucher"),
    "supply_type": ("supply type", "supply category", "nature of supply", "type of supply", "goods service", "goods or services", "item type", "transaction category", "document category", "gstr 1 category"),
    "invoice_series": ("invoice series", "series", "invoice prefix", "bill series"),
    "original_invoice_number": ("original invoice number", "original invoice no", "original bill no", "against invoice no", "reference invoice number"),
    "invoice_status": ("invoice status", "document status", "voucher status", "status"),
    "description": ("description", "item description", "product description", "service description", "particulars", "item name", "product name", "service name"),
    "quantity": ("quantity", "qty", "total quantity", "invoice quantity", "item quantity", "sum of item quantity", "sum of quantity"),
    "uqc": ("uqc", "unit quantity code", "unit code", "unit of measurement"),
    "export_type": ("export type", "export under", "lut or bond", "shipping bill type", "payment of igst"),
    "shipping_bill_number": ("shipping bill number", "shipping bill no", "sb number", "bill of export number"),
    "shipping_bill_date": ("shipping bill date", "shipping bill dt", "sb date", "bill of export date"),
    "port_code": ("port code", "port of export", "customs port code"),
    "note_reason": ("note reason", "credit note reason", "debit note reason", "reason for note", "adjustment reason"),
    "original_invoice_date": ("original invoice date", "original invoice dt", "original bill date"),
}

REQUIRED_FIELDS = ("invoice_number", "invoice_date", "taxable_value")
OPTIONAL_FIELDS = tuple(field for field in ALIASES if field not in REQUIRED_FIELDS)


class AmbiguousColumnError(ValueError):
    def __init__(self, column: str):
        self.column = column
        super().__init__("A date column needs clarification before this file can be processed.")


class UnresolvedColumnsError(ValueError):
    def __init__(self, fields: list[str], available_columns: list[str]):
        self.fields = fields
        self.available_columns = available_columns
        super().__init__("We couldn't identify some important sales information in this file.")


class AmbiguousBusinessColumnError(ValueError):
    """A true row-level conflict requires an accountant to choose a source."""
    def __init__(self, field: str, field_label: str, columns: list[dict], affected_row_count: int, sample_rows: list[dict]):
        self.field = field
        self.field_label = field_label
        self.columns = columns
        self.affected_row_count = affected_row_count
        self.sample_rows = sample_rows
        super().__init__(f"Some rows have conflicting {field_label} values in two source columns. Review the examples and choose which column contains the value to use for the affected rows.")


class AggregatedSalesDataError(ValueError):
    """Raised when a pivot/summary is uploaded in place of invoice rows."""

    def __init__(self, indicators: list[str] | None = None):
        examples = ", ".join(f"'{value}'" for value in (indicators or [])[:3])
        reason = f" We found summary-style headings such as {examples}." if examples else " The file has summary-style totals but no usable invoice rows."
        super().__init__("This file appears to contain summarized totals rather than invoice-level transactions." + reason +
            " Upload the detailed sales register with one row per invoice or invoice line, including an invoice number, invoice date and taxable amount.")


def detect_aggregated_sales_frame(frame: pd.DataFrame) -> bool:
    """Reject pivot totals only when headers and row values both lack invoice detail.

    Some accounting systems prefix otherwise useful headings with "Sum of" or
    "Total". A header alone is not enough to reject a file: invoice identifiers,
    dates and taxable amounts in the rows are stronger evidence of detail.
    """
    normalized = [normalize_header(column) for column in frame.columns]
    aggregate_headers = sum(bool(re.match(r"^(sum|average|avg|count|minimum|maximum) of ", value))
                            for value in normalized)
    summary_headers = sum(any(token in value for token in ("grand total", "subtotal", "total invoices", "count of invoice"))
                          for value in normalized)
    field_columns = {}
    for column, header in zip(frame.columns, normalized):
        field = _ALIAS_INDEX.get(header)
        if field and field not in field_columns:
            field_columns[field] = column

    # Confirm invoice-level evidence from actual records. This prevents a
    # summarized tax heading from rejecting a detailed register whose exporter
    # simply labels amount columns as sums.
    invoice_column = field_columns.get("invoice_number")
    date_column = field_columns.get("invoice_date")
    taxable_column = field_columns.get("taxable_value")
    detailed_rows = 0
    if invoice_column is not None and date_column is not None and taxable_column is not None:
        for _, row in frame[[invoice_column, date_column, taxable_column]].head(100).iterrows():
            invoice = str(row.iloc[0] or "").strip().casefold()
            date_value = str(row.iloc[1] or "").strip()
            amount = str(row.iloc[2] or "").strip()
            if not invoice or invoice in {"total", "grand total", "subtotal", "overall total"}:
                continue
            try:
                if re.fullmatch(r"\d+(?:\.0)?", date_value) and float(date_value) > 20000:
                    date_ok = True
                else:
                    date_ok = not pd.isna(pd.to_datetime(date_value, dayfirst=True, errors="coerce"))
                amount_text = re.sub(r"(?i)(inr|rs\.?)", "", amount)
                amount_text = re.sub(r"[^0-9.+-]", "", amount_text)
                amount_ok = bool(amount_text) and Decimal(amount_text).is_finite()
            except (InvalidOperation, ValueError, TypeError):
                date_ok, amount_ok = False, False
            if date_ok and amount_ok:
                detailed_rows += 1
    if detailed_rows:
        return False

    # Even if an unusual heading prevents the taxable column from matching an
    # alias (for example "Sum of Taxable Value"), a real invoice identifier
    # paired with an invoice date is enough to avoid a premature summary-file
    # rejection. The resolver can then ask the accountant to confirm the value
    # column in business language.
    invoice_date_rows = 0
    if invoice_column is not None and date_column is not None:
        for _, row in frame[[invoice_column, date_column]].head(100).iterrows():
            invoice = str(row.iloc[0] or "").strip().casefold()
            date_value = str(row.iloc[1] or "").strip()
            if not invoice or invoice in {"total", "grand total", "subtotal", "overall total"}:
                continue
            try:
                date_ok = (bool(re.fullmatch(r"\d+(?:\.0)?", date_value)) and float(date_value) > 20000) or not pd.isna(
                    pd.to_datetime(date_value, dayfirst=True, errors="coerce"))
            except (ValueError, TypeError):
                date_ok = False
            if date_ok:
                invoice_date_rows += 1
    if invoice_date_rows:
        return False

    # A pivot/summary needs multiple independent aggregate clues, or a summary
    # heading combined with the absence of required invoice-row columns.
    invoice_fields_present = all(field in field_columns for field in ("invoice_number", "invoice_date", "taxable_value"))
    if aggregate_headers >= 2 or summary_headers >= 2:
        return True
    if aggregate_headers >= 1 and not invoice_fields_present:
        return True
    if summary_headers >= 1 and not invoice_fields_present and len(frame) <= 500:
        return True
    return False


def normalize_header(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold().strip()
    # Internal disambiguation for repeated source headings is not part of the
    # heading's business meaning (e.g. two Excel columns both named "CGST").
    text = re.sub(r"\s+\[duplicate\s+\d+\]$", "", text)
    text = text.replace("&", " and ").replace("%", " percent ")
    # Keep slash as a word separator so Invoice/Bill Date resolves identically.
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = text.split()
    normalized = []
    for token in tokens:
        token = {"amt": "amount", "amnt": "amount", "dt": "date", "num": "number", "no": "no",
                 "inv": "invoice", "doc": "document", "gstinno": "gstin"}.get(token, token)
        normalized.append(token)
    return " ".join(normalized)


def _choice_header_key(value: Any) -> str:
    """Normalize a displayed source heading while preserving duplicate ordinal."""
    text = str(value or "")
    duplicate = re.search(r"\s+\[duplicate\s+(\d+)\]$", text, flags=re.IGNORECASE)
    base = normalize_header(text)
    return f"{base} duplicate {duplicate.group(1)}" if duplicate else base


def _alias_key(value: Any) -> str:
    normalized = normalize_header(value)
    # A small, explicit list of report aggregation prefixes is understood for
    # matching field meaning. The original heading stays intact for aggregate
    # file detection, audit, and accountant review.
    return re.sub(r"^(?:sum|average|avg|minimum|maximum) of ", "", normalized)


_ALIAS_INDEX = {_alias_key(alias): field for field, aliases in ALIASES.items() for alias in aliases}


def _semantic_score(header: Any, sample: list[Any]) -> tuple[str | None, float]:
    """Infer only distinctive identifiers from values; never infer generic dates/amounts."""
    values = [str(value).strip() for value in sample if str(value).strip()]
    if not values:
        return None, 0.0
    header_tokens = set(normalize_header(header).split())
    # An original/prior/seller GSTIN is not the current recipient's GSTIN.
    if header_tokens.intersection({"original", "previous", "prior", "seller", "supplier", "vendor"}):
        return None, 0.0
    if header_tokens.intersection({"ship", "shipping", "delivery", "consignee"}) and not header_tokens.intersection({"bill", "billing"}):
        return None, 0.0
    if sum(bool(re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", value.upper())) for value in values) / len(values) >= .7:
        return "customer_gstin", .94
    if sum(bool(re.fullmatch(r"\d{4,8}", re.sub(r"\.0$", "", value))) for value in values) / len(values) >= .8:
        name = normalize_header(header)
        if any(token in name for token in ("hsn", "sac", "commodity")):
            return "hsn_sac", .94
    # Values can strengthen known meanings, but data shape alone cannot distinguish
    # invoice date/payment date or one tax component from another.
    return None, 0.0


def _fuzzy_header_match(header: str) -> tuple[str | None, float]:
    """Match headings by high-confidence token coverage and edit similarity."""
    normalized = normalize_header(header)
    tokens = set(normalized.split())
    ignored = {"of", "the", "for", "and", "to", "no", "number", "amount", "value", "total", "sum"}
    # Historical and supplier GSTINs are not the current recipient GSTIN.
    if tokens.intersection({"original", "previous", "prior", "seller", "supplier", "vendor"}):
        return None, 0.0
    best_field, best_score = None, 0.0
    for field, aliases in ALIASES.items():
        for alias in aliases:
            alias_normalized = normalize_header(alias)
            alias_tokens = set(alias_normalized.split()) - ignored
            if len(alias_tokens) < 2:
                continue
            overlap = tokens & alias_tokens
            if len(overlap) < 2:
                continue
            coverage = len(overlap) / len(alias_tokens)
            edit = SequenceMatcher(None, normalized, alias_normalized).ratio()
            score = max(edit, 0.88 + 0.12 * coverage if coverage >= 0.9 else 0.0)
            if score > best_score:
                best_field, best_score = field, score
    return (best_field, best_score) if best_score >= 0.88 else (None, best_score)


def _column_signature(series: pd.Series) -> tuple[str, ...]:
    """Compare whole columns while ignoring harmless case and punctuation differences."""
    def normalized(value: Any) -> str:
        if pd.isna(value):
            return ""
        text = unicodedata.normalize("NFKC", str(value)).casefold().strip()
        numeric = re.sub(r"(?i)(inr|rs\.?|₹|,|%|\s)", "", text)
        try:
            amount = Decimal(numeric)
            if amount.is_finite():
                return format(amount.normalize(), "f")
        except InvalidOperation:
            pass
        return re.sub(r"[^a-z0-9]+", "", text)
    return tuple(normalized(value) for value in series.tolist())


_STATE_ALIASES = {
    "ORISSA": "21", "UTTARANCHAL": "05", "DADRA AND NAGAR HAVELI": "26",
    "DAMAN AND DIU": "25", "JAMMU AND KASHMIR": "01", "PONDICHERRY": "34",
}
_STATE_CODE_BY_NAME = {name.casefold(): code for code, name in STATE_NAMES.items()}


def _normalized_state_code(value: Any) -> tuple[str | None, str]:
    """Return (GST state code, state) for comparable names/codes."""
    if pd.isna(value) or not str(value).strip():
        return None, "missing"
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value)).strip()).upper()
    code_match = re.search(r"(?<!\d)(\d{2})(?!\d)", text)
    if code_match and code_match.group(1) in STATE_NAMES:
        remainder = (text[:code_match.start()] + " " + text[code_match.end():]).strip(" -,/()")
        if not remainder or re.sub(r"[^A-Z]", "", remainder) in {"STATE", "STATEOF"}:
            return code_match.group(1), "valid"
        normalized_remainder = re.sub(r"[^A-Z ]+", " ", remainder)
        normalized_remainder = re.sub(r"\s+", " ", normalized_remainder).strip()
        if normalized_remainder.startswith("STATE OF "):
            normalized_remainder = normalized_remainder[9:]
        if normalized_remainder.endswith(" STATE"):
            normalized_remainder = normalized_remainder[:-6]
        if normalized_remainder.casefold() in _STATE_CODE_BY_NAME or normalized_remainder in _STATE_ALIASES:
            return code_match.group(1), "valid"
    for name, code in _STATE_CODE_BY_NAME.items():
        normalized_name = re.sub(r"[^A-Z ]+", " ", text)
        normalized_name = re.sub(r"\s+", " ", normalized_name).strip()
        if normalized_name.startswith("STATE OF "):
            normalized_name = normalized_name[9:]
        if normalized_name.endswith(" STATE"):
            normalized_name = normalized_name[:-6]
        if normalized_name.casefold() == name:
            return code, "valid"
    alias = _STATE_ALIASES.get(re.sub(r"[^A-Z ]+", " ", text).strip())
    return (alias, "valid") if alias else (None, "invalid")


def _canonical_column_value(field: str, value: Any) -> tuple[str | None, str]:
    if pd.isna(value) or not str(value).strip():
        return None, "missing"
    if field == "place_of_supply":
        return _normalized_state_code(value)
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if field in {"taxable_value", "cgst", "sgst", "igst", "cess", "invoice_value", "quantity", "gst_rate"}:
        try:
            number = normalize_percentage(text) if field == "gst_rate" else normalize_money(text)
            return format(number.normalize(), "f"), "valid"
        except ValueError:
            return re.sub(r"\s+", " ", text).casefold(), "invalid"
    if field == "invoice_date":
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                return pd.Timestamp(text).date().isoformat(), "valid"
            parsed = pd.to_datetime(text, dayfirst=True, errors="raise")
            return parsed.date().isoformat(), "valid"
        except (ValueError, TypeError):
            return re.sub(r"\s+", " ", text).casefold(), "invalid"
    return re.sub(r"[^a-z0-9]+", "", text.casefold()), "valid"


def _is_overall_gst_rate_column(frame: pd.DataFrame, rate_column: Any, resolved: dict[str, str]) -> bool:
    """Use tax arithmetic to disambiguate a generic "Rate" header safely."""
    source_by_field = {field: source for source, field in resolved.items()}
    taxable_column = source_by_field.get("taxable_value")
    tax_columns = [source_by_field[field] for field in ("cgst", "sgst", "igst") if field in source_by_field]
    if taxable_column is None or not tax_columns:
        return False
    matches = considered = 0
    for _, row in frame[[rate_column, taxable_column, *tax_columns]].head(50).iterrows():
        try:
            rate = normalize_percentage(row[rate_column])
            taxable = normalize_money(row[taxable_column])
            tax = sum((normalize_money(row[column]) for column in tax_columns), Decimal("0.00"))
        except (ValueError, InvalidOperation):
            continue
        if taxable <= 0 or tax <= 0:
            continue
        expected_rate = (tax * Decimal("100") / taxable).quantize(Decimal("0.01"))
        considered += 1
        if abs(rate - expected_rate) <= Decimal("0.25"):
            matches += 1
    return considered > 0 and matches / considered >= 0.8


def format_fingerprint(columns: list[Any], source_type: str) -> str:
    structure = "\x1f".join([source_type.casefold(), *(normalize_header(col) for col in columns)])
    return hashlib.sha256(structure.encode("utf-8")).hexdigest()


def resolve_columns(frame: pd.DataFrame, source_type: str, known_mapping: dict[str, str] | None = None,
                    confirmed_columns: dict[str, str] | None = None,
                    business_choices: dict[str, str] | None = None) -> dict[str, Any]:
    known = known_mapping or {}
    confirmed = {normalize_header(header): field for header, field in (confirmed_columns or {}).items()}
    explicit_business = {_choice_header_key(header): field for header, field in (business_choices or {}).items()}
    resolved: dict[str, str] = {}
    evidence: dict[str, str] = {}
    candidates_by_field: dict[str, list[tuple[str, float, str]]] = {}
    generic_rate_columns = []
    normalized_known = {normalize_header(header): field for header, field in known.items()}
    for column in frame.columns:
        label = str(column)
        norm = normalize_header(label)
        field = confirmed.get(norm)
        confidence = 1.0 if field else 0.0
        if field == "customer_gstin" and norm not in confirmed and set(norm.split()).intersection(
                {"original", "previous", "prior", "seller", "supplier", "vendor"}):
            # Do not let a rule learned from another export turn a historical
            # or seller GSTIN into the current recipient's GSTIN.
            field = None
        if not field:
            field = _ALIAS_INDEX.get(_alias_key(label))
            if field:
                evidence[label] = "header rule"
                confidence = 1.0
        if not field:
            field = normalized_known.get(norm)
            if field in ALIASES:
                evidence[label] = "client profile"
                confidence = 0.95
            else:
                field = None
        if not field:
            if norm == "rate":
                generic_rate_columns.append(label)
            field, confidence = _fuzzy_header_match(label)
            if field == "customer_gstin" and {"ship", "shipping", "delivery", "consignee"}.intersection(norm.split()) and not {"bill", "billing"}.intersection(norm.split()):
                # A shipping/consignee registration can differ from the billed
                # recipient. It is not the recipient GSTIN without explicit
                # billing semantics in the heading.
                field = None
            if field:
                evidence[label] = "similar heading"
        if not field:
            sample = frame[column].head(50).tolist()
            field, confidence = _semantic_score(label, sample)
            if field and confidence >= .9:
                evidence[label] = "distinctive value pattern"
        if not field:
            continue
        candidates_by_field.setdefault(field, []).append((label, confidence, evidence.get(label, "header rule")))

    # Compare repeated candidates row by row. A previous profile can suggest a
    # heading, but it cannot suppress a newly observed value conflict.
    merged_values: dict[str, list[Any]] = {}
    resolution_decisions: list[dict] = []
    row_diagnostics: list[dict] = []
    field_labels = {"customer_name": "customer name", "customer_gstin": "recipient GSTIN",
        "invoice_number": "invoice number", "invoice_date": "invoice date", "taxable_value": "taxable amount",
        "cgst": "CGST amount", "sgst": "SGST amount", "igst": "IGST amount", "cess": "cess amount",
        "invoice_value": "invoice total", "place_of_supply": "place of supply", "hsn_sac": "HSN/SAC",
        "gst_rate": "GST rate", "document_type": "document type", "supply_type": "supply category",
        "original_invoice_number": "original invoice reference", "invoice_status": "invoice status",
        "description": "item description", "quantity": "quantity", "uqc": "unit of measure",
        "export_type": "export type", "shipping_bill_number": "shipping bill number",
        "shipping_bill_date": "shipping bill date", "port_code": "port code", "note_reason": "note reason"}
    for field, candidates in candidates_by_field.items():
        labels = [item[0] for item in candidates]
        if len(candidates) == 1:
            label, _, reason = candidates[0]
            resolved[label] = field
            evidence[label] = reason
            continue

        canonical = [[_canonical_column_value(field, value) for value in frame[label].tolist()] for label in labels]
        conflict_positions = []
        invalid_positions: dict[int, list[tuple[str, Any]]] = {}
        for position in range(len(frame)):
            present = [(labels[c], canonical[c][position][0], canonical[c][position][1], frame[labels[c]].iloc[position])
                for c in range(len(labels)) if canonical[c][position][1] != "missing"]
            valid_values = {entry[1] for entry in present if entry[2] == "valid" and entry[1] is not None}
            invalid_values = [entry for entry in present if entry[2] == "invalid"]
            if field == "place_of_supply" and valid_values:
                # A valid state in a semantically equivalent POS column may
                # fill a missing/invalid duplicate, but preserve an issue for
                # any malformed supplied value below.
                if len(valid_values) > 1:
                    conflict_positions.append(position)
            elif len({entry[1] for entry in present}) > 1:
                conflict_positions.append(position)
            if invalid_values and field == "place_of_supply" and valid_values:
                invalid_positions[position] = [(entry[0], entry[3]) for entry in invalid_values]

        explicit = next((label for label in labels if explicit_business.get(_choice_header_key(label)) == field), None)
        if conflict_positions and not explicit:
            conflict_rows = []
            for position in conflict_positions[:8]:
                row_number = int(frame.index[position]) + 2
                conflict_rows.append({"row_number": row_number,
                    "values": {label: str(frame[label].iloc[position]) for label in labels}})
            choice_columns = []
            for label in labels:
                populated_for_all = all(canonical[labels.index(label)][position][1] != "missing"
                    for position in conflict_positions)
                if populated_for_all:
                    choice_columns.append({"name": label, "examples": [row["values"][label]
                        for row in conflict_rows]})
            raise AmbiguousBusinessColumnError(field, field_labels.get(field, "sales detail"),
                [{"name": item["name"], "examples": item["examples"]} for item in choice_columns],
                len(conflict_positions), conflict_rows)

        if explicit:
            chosen = explicit
            row_values = []
            for position in range(len(frame)):
                if position in conflict_positions:
                    row_values.append(frame[chosen].iloc[position])
                else:
                    available = [frame[label].iloc[position] for label in labels
                        if canonical[labels.index(label)][position][1] != "missing"]
                    row_values.append(available[0] if available else "")
            decision = "ACCOUNTANT_CONFIRMED_SOURCE"
            resolution_decisions.append({"business_field": field_labels.get(field, field),
                "source_columns": labels, "selected_column": chosen, "decision": decision,
                "affected_row_count": len(conflict_positions),
                "affected_source_rows": [int(frame.index[position]) + 2 for position in conflict_positions]})
        else:
            chosen = labels[0]
            row_values = []
            missing_merge_count = 0
            equivalent_count = 0
            for position in range(len(frame)):
                available = [(label, frame[label].iloc[position], canonical[index][position])
                    for index, label in enumerate(labels) if canonical[index][position][1] != "missing"]
                if len(available) > 1:
                    equivalent_count += 1
                if len(available) < len(labels):
                    missing_merge_count += 1
                if field == "place_of_supply":
                    valid = next((entry for entry in available if entry[2][1] == "valid"), None)
                    value = valid[2][0] if valid else (available[0][1] if available else "")
                else:
                    value = available[0][1] if available else ""
                row_values.append(value)
            if invalid_positions:
                decision = "VALID_STATE_SELECTED_INVALID_ALTERNATIVE_RECORDED"
            elif missing_merge_count:
                decision = "MERGED_MISSING_VALUES"
            else:
                decision = "AUTO_EQUIVALENT_COLUMNS"
            resolution_decisions.append({"business_field": field_labels.get(field, field),
                "source_columns": labels, "selected_column": chosen, "decision": decision,
                "affected_row_count": missing_merge_count if missing_merge_count else equivalent_count})

        resolved[chosen] = field
        evidence[chosen] = "accountant-confirmed source" if explicit else candidates[0][2]
        # Carry a merged canonical column without changing the retained source.
        merged_values[field] = row_values
        for position, sources in invalid_positions.items():
            for source_header, raw_value in sources:
                row_diagnostics.append({"row_number": int(frame.index[position]) + 2,
                    "field": field_labels.get(field, field), "source_column": source_header,
                    "raw_value": str(raw_value), "code": "PLACE_OF_SUPPLY_INVALID_SOURCE"})

    # "Rate" is not a safe alias on its own: it may mean a unit price or
    # discount. Resolve it as the invoice GST rate only when the supplied tax
    # amounts support that interpretation across the rows.
    if "gst_rate" not in resolved.values():
        for rate_column in generic_rate_columns:
            if _is_overall_gst_rate_column(frame, rate_column, resolved):
                resolved[rate_column] = "gst_rate"
                evidence[rate_column] = "tax arithmetic and neighboring tax fields"
                break

    for header, field in explicit_business.items():
        if not any(_choice_header_key(label) == header and candidate_field == field
                   for candidate_field, candidates in candidates_by_field.items() for label, _, _ in candidates):
            raise ValueError("Choose one of the source columns shown in the conflict review.")

    missing = [field for field in REQUIRED_FIELDS if field not in resolved.values()]
    if missing:
        if "invoice_date" in missing:
            selected_date_meaning = confirmed.get("date")
            if selected_date_meaning and selected_date_meaning != "invoice_date":
                raise ValueError("The selected date is not the invoice date. Choose invoice date only if this column truly contains it, or upload a register that includes invoice dates.")
            ambiguous_date = next((str(column) for column in frame.columns
                                   if normalize_header(column) == "date"), None)
            if ambiguous_date and not confirmed.get("date"):
                raise AmbiguousColumnError(ambiguous_date)
        public_labels = {"invoice_number": "invoiceNumber", "invoice_date": "invoiceDate", "taxable_value": "taxableAmount"}
        raise UnresolvedColumnsError([public_labels[field] for field in missing],
            [str(column) for column in frame.columns if str(column) not in resolved])
    fingerprint = format_fingerprint(list(frame.columns), source_type)
    return {"mapping": resolved, "evidence": evidence, "fingerprint": fingerprint,
            "alias_version": ALIAS_VERSION, "source_type": source_type.casefold(),
            "confirmed_columns": {**dict(confirmed_columns or {}), **dict(business_choices or {})}, "merged_values": merged_values,
            "resolution_decisions": resolution_decisions, "row_diagnostics": row_diagnostics}


def normalize_money(value: Any) -> Decimal:
    text = str(value or "").strip()
    if not text:
        return Decimal("0.00")
    negative = text.startswith("(") and text.endswith(")")
    text = re.sub(r"(?i)(inr|rs\.?)", "", text)
    # Accept currency symbols/codes and locale grouping marks without tying
    # normalization to a particular client export's rupee glyph encoding.
    text = re.sub(r"[^0-9.+-]", "", text.strip("() "))
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Amount contains an unrecognized value") from exc
    if not result.is_finite():
        raise ValueError("Amount must be a finite number")
    if negative:
        result = -abs(result)
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalize_percentage(value: Any) -> Decimal:
    text = str(value or "").strip().replace("%", "").strip()
    if not text:
        return Decimal("0.00")
    try:
        result = Decimal(text)
        if not result.is_finite() or result < 0 or result > 100:
            raise ValueError("Tax rate must be between 0 and 100 percent")
        return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Tax rate contains an unrecognized value") from exc


def normalize_quantity(value: Any) -> Decimal:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return Decimal("0")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Quantity contains an unrecognized value") from exc
    if not number.is_finite():
        raise ValueError("Quantity must be finite")
    return number.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def read_sales_frame(contents: bytes, filename: str) -> tuple[pd.DataFrame, str]:
    """Read the first worksheet/CSV and detect a header row in a short preamble."""
    lowered = filename.casefold()
    if lowered.endswith(".csv"):
        if not contents or not contents.strip():
            raise ValueError("The selected CSV is empty. Save the sales register with its headings and invoice rows, then upload it again.")
        if contents.startswith(b"PK\x03\x04"):
            raise ValueError("This file is an Excel workbook saved with a .csv name. Save it as CSV UTF-8 or upload the .xlsx workbook.")
        decoded = None
        for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                decoded = contents.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise ValueError("The CSV text encoding could not be read. In Excel, save a fresh copy as CSV UTF-8 and upload it again.")

        # Excel and accounting systems commonly export comma, semicolon,
        # tab, or pipe-delimited CSVs. Try each safely and select the parse
        # whose rows contain the most recognizable sales headings.
        delimiters = [",", ";", "\t", "|"]
        try:
            import csv
            sniffed = csv.Sniffer().sniff(decoded[:65536], delimiters=",;\t|").delimiter
            delimiters = [sniffed, *[item for item in delimiters if item != sniffed]]
        except csv.Error:
            pass
        parsed_candidates = []
        for delimiter in delimiters:
            try:
                candidate = pd.read_csv(io.StringIO(decoded), dtype=str, keep_default_na=False,
                    header=None, sep=delimiter, engine="python", on_bad_lines="error")
            except (pd.errors.ParserError, UnicodeError, ValueError):
                continue
            if candidate.empty:
                continue
            header_hits = 0
            for row_index in range(min(len(candidate), 15)):
                header_hits = max(header_hits, sum(normalize_header(value) in _ALIAS_INDEX
                    for value in candidate.iloc[row_index].tolist()))
            populated = int(candidate.notna().sum().sum())
            parsed_candidates.append((header_hits, candidate.shape[1], populated, candidate))
        if not parsed_candidates:
            raise ValueError("We couldn't read the CSV rows. Check that every row uses the same separator and that quoted values are closed, then save as CSV UTF-8 and retry.")
        raw = max(parsed_candidates, key=lambda item: (item[0], item[1], item[2]))[3]
        source_type = "csv"
    elif lowered.endswith((".xlsx", ".xls")):
        if not contents:
            raise ValueError("The selected Excel workbook is empty. Save the workbook with its sales register sheet and upload it again.")
        try:
            raw = pd.read_excel(io.BytesIO(contents), dtype=str, keep_default_na=False, header=None)
        except ImportError as exc:
            if lowered.endswith(".xls"):
                raise ValueError("This legacy .xls workbook needs an Excel .xlsx or CSV UTF-8 copy. Open it in Excel and use Save As.") from exc
            raise ValueError("The Excel workbook reader is unavailable. Save a copy as CSV UTF-8 and upload it instead.") from exc
        except (ValueError, OSError) as exc:
            raise ValueError("The Excel file could not be opened. Confirm it is a valid .xlsx workbook, then save or export it again.") from exc
        source_type = "excel"
    else:
        raise ValueError("Upload a CSV or Excel sales register.")
    if raw.empty:
        raise ValueError("The uploaded sales register does not contain any rows.")
    known_names = set(_ALIAS_INDEX)
    best_row, best_hits = 0, -1
    for row_index in range(min(len(raw), 15)):
        values = [normalize_header(value) for value in raw.iloc[row_index].tolist()]
        hits = sum(value in known_names for value in values)
        if hits > best_hits:
            best_row, best_hits = row_index, hits
    labels = [str(value).strip() or f"Unnamed column {index + 1}" for index, value in enumerate(raw.iloc[best_row].tolist())]
    # DataFrame label lookup returns a DataFrame when headers repeat. Give
    # repeated headings stable internal suffixes so each source column can be
    # compared independently and an identical duplicate can be discarded.
    occurrences: dict[str, int] = {}
    unique_labels = []
    for label in labels:
        occurrences[label] = occurrences.get(label, 0) + 1
        unique_labels.append(label if occurrences[label] == 1 else f"{label} [duplicate {occurrences[label]}]")
    labels = unique_labels
    frame = raw.iloc[best_row + 1:].copy()
    frame.columns = labels
    # Keep the original zero-based source row so exception row numbers still
    # point to the uploaded workbook/CSV even when a title preamble exists.
    frame.index = range(best_row, best_row + len(frame))
    return frame, source_type


def apply_resolution(frame: pd.DataFrame, resolution: dict[str, Any]) -> pd.DataFrame:
    """Return a canonical-only frame; raw source bytes remain separately preserved."""
    rename = resolution["mapping"]
    normalized = frame.loc[:, list(rename)].rename(columns=rename).copy()
    for field, values in resolution.get("merged_values", {}).items():
        normalized[field] = pd.Series(values, index=normalized.index)
    # Combine a separately exported series/prefix with the invoice number only
    # when the invoice number does not already carry that prefix.
    if "invoice_series" in normalized.columns and "invoice_number" in normalized.columns:
        prefixes = normalized["invoice_series"].fillna("").astype(str).str.strip()
        numbers = normalized["invoice_number"].fillna("").astype(str).str.strip()
        normalized["invoice_number"] = [f"{prefix}-{number}" if prefix and number and not number.casefold().startswith(prefix.casefold()) else number
                                        for prefix, number in zip(prefixes, numbers)]
    return normalized
