"""Outward sales register parsing and deterministic GSTR-1 draft checks.

Classification is explanatory preparation, not a legal determination. Threshold
selection is based on the invoice tax period and records the rule version used.
"""
import hashlib
import re
from difflib import SequenceMatcher
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd

from app.ingestion.column_resolution import (
    AggregatedSalesDataError, AmbiguousBusinessColumnError, AmbiguousColumnError, apply_resolution, detect_aggregated_sales_frame,
    format_fingerprint, normalize_money, normalize_percentage, normalize_quantity, read_sales_frame, resolve_columns,
)
from app.matching.normalize import normalize_invoice_number
from app.validation import STATE_NAMES, normalize_gstin, validate_gstin

CENT = Decimal("0.01")


def period_month(period: str) -> date:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period or ""):
        raise ValueError("Tax period must use YYYY-MM format.")
    return date.fromisoformat(f"{period}-01")


def b2cl_threshold(period: str, rules: list[dict]) -> tuple[Decimal, str]:
    """Resolve the historical B2CL threshold from effective-dated reference data."""
    effective = period_month(period)
    for rule in rules:
        start = date.fromisoformat(rule["effective_from"])
        end = date.fromisoformat(rule["effective_to"]) if rule.get("effective_to") else None
        if start <= effective and (end is None or effective <= end):
            return Decimal(str(rule["definition"]["threshold_inr"])), rule["version"]
    raise ValueError(f"No verified B2CL threshold rule is configured for {period}.")


def normalize_place_of_supply(raw: str | None) -> str | None:
    """Return a GST state code for either a code or a recognizable state name."""
    value = re.sub(r"\s+", " ", (raw or "").strip()).upper()
    if not value:
        return None
    match = re.match(r"^(\d{2})(?:\D|$)", value)
    if match and match.group(1) in STATE_NAMES:
        return match.group(1)
    for code, name in STATE_NAMES.items():
        if value == name.upper():
            return code
    aliases = {"ORISSA": "21", "UTTARANCHAL": "05", "DADRA AND NAGAR HAVELI": "26", "DAMAN AND DIU": "25"}
    return aliases.get(value)


def normalize_document_type(raw: str | None) -> str:
    """Collapse common accounting labels into stable reporting document types."""
    text = re.sub(r"[^A-Z0-9]+", " ", (raw or "").upper()).strip()
    if not text:
        text = "INVOICE"
    if "CREDIT" in text and ("NOTE" in text or "MEMO" in text) or text in {"CN", "CRN"}:
        return "CREDIT_NOTE_FINANCIAL" if "FINANCIAL" in text else "CREDIT_NOTE"
    if "DEBIT" in text and ("NOTE" in text or "MEMO" in text) or text in {"DN", "DRN"}:
        return "DEBIT_NOTE_FINANCIAL" if "FINANCIAL" in text else "DEBIT_NOTE"
    if "DEEMED EXPORT" in text:
        return "DEEMED_EXPORT"
    if "SEZ" in text or "SPECIAL ECONOMIC ZONE" in text:
        return "SEZ"
    if "EXPORT" in text:
        return "EXPORT"
    if text in {"INVOICE", "TAX INVOICE", "SALES INVOICE", "BILL", "SALES BILL", "INVOICE BILL"} or "INVOICE" in text or text in {"SALE", "SALES"}:
        return "INVOICE"
    if text in {"OTHER", "OTHERS", "JOURNAL", "ADJUSTMENT"}:
        return "OTHER"
    return "OTHER"


def normalize_supply_type(raw: str | None) -> str:
    text = re.sub(r"[^A-Z0-9]+", " ", (raw or "").upper()).strip()
    if "BRANCH" in text and ("TRANSFER" in text or "TRANSF" in text):
        return "BRANCH_TRANSFER"
    if "EMPLOYEE" in text and ("REIMB" in text or "CATERER" in text):
        return "EMPLOYEE_REIMBURSEMENT"
    if "DUTY SCRIPT" in text or "EXEMPT" in text or "NIL RATED" in text:
        return "EXEMPT"
    if "EXPORT SERVICE" in text or ("EXPORT" in text and "SERVICE" in text):
        return "EXPORT_SERVICE"
    if not text or "GOOD" in text:
        return "GOODS"
    if "DEEMED EXPORT" in text:
        return "DEEMED_EXPORT"
    if "SEZ" in text or "SPECIAL ECONOMIC ZONE" in text:
        return "SEZ"
    if "EXPORT" in text:
        return "EXPORT"
    if "SERVICE" in text:
        return "SERVICES"
    return "OTHER"


def _separate_source_document_and_category(raw_document: str, raw_supply: str) -> tuple[str, str, str | None]:
    """Interpret accounting exports that place B2B/B2C/Export in a type column.

    The source column is sometimes named "Document Type" even when it contains
    the supply category. Preserve real invoice/note semantics and move a clear
    supply label into the category side of the model.
    """
    tokens = re.sub(r"[^A-Z0-9]+", " ", f"{raw_document or ''} {raw_supply or ''}".upper()).strip()
    compact = " ".join(tokens.split())
    declared_category = None
    if re.search(r"\bB2B\b", compact):
        declared_category = "B2B"
    elif re.search(r"\bB2C(?:S|L)?\b|\bUNREGISTERED\b|\bURD\b", compact):
        declared_category = "B2C"
    elif "DEEMED EXPORT" in compact:
        declared_category = "DEEMED_EXPORT"
    elif "SEZ" in compact or "SPECIAL ECONOMIC ZONE" in compact:
        declared_category = "SEZ"
    elif "EXPORT" in compact and "SERVICE" in compact:
        declared_category = "EXPORT_SERVICE"
    elif "EXPORT" in compact:
        declared_category = "EXPORT"
    elif "BRANCH" in compact and ("TRANSFER" in compact or "TRANSF" in compact):
        declared_category = "BRANCH_TRANSFER"
    elif "EMPLOYEE" in compact and ("REIMB" in compact or "CATERER" in compact):
        declared_category = "EMPLOYEE_REIMBURSEMENT"
    elif "EXEMPT" in compact or "NIL RATED" in compact or "DUTY SCRIPT" in compact:
        declared_category = "EXEMPT"

    has_note_document = normalize_document_type(raw_document).startswith(("CREDIT_NOTE", "DEBIT_NOTE"))
    supply_value = raw_supply
    if not supply_value and declared_category in {"EXPORT", "EXPORT_SERVICE", "SEZ", "DEEMED_EXPORT", "BRANCH_TRANSFER", "EMPLOYEE_REIMBURSEMENT", "EXEMPT"}:
        supply_value = declared_category
    if declared_category and not has_note_document:
        # A category label is not a document kind. For these rows the actual
        # document is a sales invoice unless the file explicitly says note.
        return "INVOICE", supply_value, declared_category
    return normalize_document_type(raw_document), supply_value, declared_category


def reporting_category(document_type: str | None, supply_type: str | None, classification: str | None, *,
                       seller_gstin: str | None = None, customer_gstin: str | None = None,
                       place_of_supply: str | None = None, invoice_value: Decimal | None = None,
                       period: str | None = None, b2cl_rules: list[dict] | None = None) -> str:
    doc = normalize_document_type(document_type)
    supply = normalize_supply_type(supply_type)
    if doc in {"CREDIT_NOTE", "CREDIT_NOTE_FINANCIAL"}:
        return "CREDIT_NOTE"
    if doc in {"DEBIT_NOTE", "DEBIT_NOTE_FINANCIAL"}:
        return "DEBIT_NOTE"
    if doc in {"EXPORT", "SEZ", "DEEMED_EXPORT"}:
        return doc
    if supply == "DEEMED_EXPORT":
        return "DEEMED_EXPORT"
    if supply in {"SEZ", "EXPORT", "EXPORT_SERVICE"}:
        return "EXPORT" if supply == "EXPORT_SERVICE" else supply
    if supply == "EXEMPT":
        return "EXEMPT"
    if doc == "OTHER":
        if classification and classification != "OTHER":
            return classification
        if seller_gstin and period and b2cl_rules is not None:
            try:
                return classify_invoice(seller_gstin=seller_gstin, customer_gstin=customer_gstin,
                    place_of_supply=place_of_supply, invoice_value=invoice_value or Decimal("0"),
                    period=period, b2cl_rules=b2cl_rules, document_type="INVOICE")[0]
            except ValueError:
                # Lack of an effective threshold reference should not turn a
                # GSTIN-known supply into an invented reporting subtype.
                if customer_gstin:
                    valid, _ = validate_gstin(customer_gstin)
                    return "B2B" if valid else "UNCLASSIFIED"
                return "B2CS"
        return "UNCLASSIFIED"
    return classification or "UNCLASSIFIED"


def _reference_rates_for_hsn(definition: dict, hsn_sac: str) -> list[Decimal]:
    rate_map = definition.get("rates_by_hsn", definition.get("rates", {}))
    if not isinstance(rate_map, dict):
        return []
    entry = next((value for key, value in rate_map.items() if str(key).strip().upper() == hsn_sac.strip().upper()), None)
    if isinstance(entry, dict):
        entry = entry.get("rates", entry.get("rate"))
    values = entry if isinstance(entry, (list, tuple, set)) else [entry]
    parsed = []
    for value in values:
        if value is None:
            continue
        try:
            rate = Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
        if rate.is_finite() and Decimal("0") <= rate <= Decimal("100"):
            parsed.append(rate)
    return sorted(set(parsed))


def classify_invoice(*, seller_gstin: str, customer_gstin: str | None,
                     place_of_supply: str | None, invoice_value: Decimal,
                     period: str, b2cl_rules: list[dict], document_type: str = "INVOICE",
                     supply_type: str | None = None) -> tuple[str, str, str]:
    doc = normalize_document_type(document_type)
    supply = normalize_supply_type(supply_type)
    if doc in {"CREDIT_NOTE", "CREDIT_NOTE_FINANCIAL"}:
        return "CREDIT_NOTE", "Credit note document type supplied", "GST_PORTAL_GSTR1_CURRENT"
    if doc in {"DEBIT_NOTE", "DEBIT_NOTE_FINANCIAL"}:
        return "DEBIT_NOTE", "Debit note document type supplied", "GST_PORTAL_GSTR1_CURRENT"
    if doc == "EXPORT" or supply == "EXPORT":
        return "EXPORT", "Export document type supplied", "GST_PORTAL_GSTR1_CURRENT"
    if doc == "SEZ" or supply == "SEZ":
        return "SEZ", "SEZ supply indicated in source document or supply type; reporting treatment requires review", "GST_PORTAL_GSTR1_CURRENT"
    if doc == "DEEMED_EXPORT" or supply == "DEEMED_EXPORT":
        return "DEEMED_EXPORT", "Deemed export indicated in source data; reporting treatment requires review", "GST_PORTAL_GSTR1_CURRENT"
    customer = normalize_gstin(customer_gstin or "")
    if customer:
        ok, _ = validate_gstin(customer)
        if ok:
            return "B2B", "B2B — valid recipient GSTIN found (format and checksum pass); registration status should be verified against an authoritative source.", "GST_PORTAL_GSTR1_CURRENT"
        return "UNCLASSIFIED", "A recipient GSTIN was supplied but failed validation; review the GSTIN before assigning B2B or B2C.", "GST_PORTAL_GSTR1_CURRENT"
    seller_state = (seller_gstin or "")[:2]
    destination = normalize_place_of_supply(place_of_supply) or None
    if destination and seller_state and destination != seller_state:
        threshold, version = b2cl_threshold(period, b2cl_rules)
        if invoice_value > threshold:
            return "B2CL", f"B2C — recipient GSTIN unavailable; place of supply is inter-State and invoice value exceeds ₹{threshold:,.0f} threshold.", version
        return "B2CS", f"B2C — recipient GSTIN unavailable; place of supply is inter-State and invoice value is at or below ₹{threshold:,.0f} threshold.", version
    if destination and seller_state:
        return "B2CS", "B2C — recipient GSTIN unavailable; place of supply is in the seller's State.", "GST_PORTAL_GSTR1_CURRENT"
    return "B2CS", "B2C — recipient GSTIN unavailable; grouped as B2C. Place of supply was not supplied, so confirm the B2C reporting subtype.", "GST_PORTAL_GSTR1_CURRENT"


def _customer_key(row: dict) -> str:
    gstin = row.get("customer_gstin") or row.get("customerGstin")
    if gstin:
        return str(gstin).strip().upper()
    name = row.get("customer_name") or row.get("customerName") or ""
    return re.sub(r"[^a-z0-9]+", "", str(name).casefold())


def validate_note_relationships(rows: list[dict], issues: list[dict], historical_invoices: list[dict] | None = None,
                              period: str | None = None) -> dict[int, dict]:
    """Resolve note-to-invoice links across the complete upload and prior registers.

    Link and amount checks happen after parsing all rows so a note can reference
    an invoice that appears later in the file and multi-line documents are
    compared by their combined values.
    """
    historical_invoices = historical_invoices or []

    def field(record: dict, key: str, model_key: str | None = None):
        return record.get(key) if isinstance(record, dict) else getattr(record, model_key or key, None)

    original_groups = {}
    for row in rows:
        doc = normalize_document_type(row.get("document_type"))
        status = str(row.get("invoice_status") or "ACTIVE").upper()
        if doc.startswith(("CREDIT_NOTE", "DEBIT_NOTE")) or doc == "OTHER":
            continue
        key = (_customer_key(row), normalize_invoice_number(row.get("invoice_number", "")), row.get("invoice_date"), "current")
        group = original_groups.setdefault(key, {"customer": key[0], "number": key[1], "date": key[2], "period": None,
            "invoice_number": row.get("invoice_number"), "invoice_value": Decimal("0"), "_sourceInvoiceValues": [], "taxable_value": Decimal("0"),
            "tax": Decimal("0"), "count": 0, "cancelled": False, "source": "current upload"})
        group["cancelled"] = group["cancelled"] or status in {"CANCELLED", "CANCELED", "VOID"}
        group["_sourceInvoiceValues"].append(row["invoice_value"])
        group["taxable_value"] += row["taxable_value"]
        group["tax"] += row["cgst"] + row["sgst"] + row["igst"] + row["cess"]
        group["count"] += 1
    for row in historical_invoices:
        doc = normalize_document_type(field(row, "document_type"))
        status = str(field(row, "invoice_status") or "ACTIVE").upper()
        if doc.startswith(("CREDIT_NOTE", "DEBIT_NOTE")) or doc == "OTHER":
            continue
        invoice_number = field(row, "invoice_number")
        normalized = field(row, "normalized_invoice_number") or normalize_invoice_number(invoice_number or "")
        customer = _customer_key({"customer_gstin": field(row, "customer_gstin"), "customer_name": field(row, "customer_name")})
        invoice_date = field(row, "invoice_date")
        period = field(row, "period") or "prior period"
        key = (customer, normalized, invoice_date, str(period))
        group = original_groups.setdefault(key, {"customer": customer, "number": normalized, "date": invoice_date, "period": str(period),
            "invoice_number": invoice_number, "invoice_value": Decimal("0"), "_sourceInvoiceValues": [], "taxable_value": Decimal("0"),
            "tax": Decimal("0"), "count": 0, "cancelled": False, "source": str(period)})
        group["cancelled"] = group["cancelled"] or status in {"CANCELLED", "CANCELED", "VOID"}
        group["_sourceInvoiceValues"].append(Decimal(str(field(row, "invoice_value") or 0)))
        group["taxable_value"] += Decimal(str(field(row, "taxable_value") or 0))
        group["tax"] += sum((Decimal(str(field(row, name) or 0)) for name in ("cgst", "sgst", "igst", "cess")), Decimal("0"))
        group["count"] += 1
    for group in original_groups.values():
        source_values = group.pop("_sourceInvoiceValues")
        group["invoice_value"] = (source_values[0] if len(source_values) > 1 and len(set(source_values)) == 1
            else sum(source_values, Decimal("0")))

    note_rows = [row for row in rows if normalize_document_type(row.get("document_type")).startswith(("CREDIT_NOTE", "DEBIT_NOTE"))]
    links: dict[int, dict | None] = {}
    notes_by_original = {}
    historical_notes = []
    for row in historical_invoices:
        doc = normalize_document_type(field(row, "document_type"))
        if doc.startswith("CREDIT_NOTE"):
            historical_notes.append(row)
    for row in note_rows:
        row_number = row["row_number"]

        def add_note_issue(code: str, message: str):
            if any(item.get("code") == code for item in row["issues"]):
                return
            issue = {"code": code, "severity": "REVIEW", "message": message}
            row["issues"].append(issue)
            row["status"] = "SYSTEM_FLAGGED"
            issues.append({"row": row_number, **issue})

        add_note_issue("NOTE_PORTAL_REPORTING_CHECK",
            "This application cannot read the GST Portal's filed-return history. Check this note in filed GSTR-1 before including it again; record confirmation only after checking the portal or filing records.")
        customer = _customer_key(row)
        prior_same_note = [item for item in historical_notes if _customer_key({
            "customer_gstin": field(item, "customer_gstin"), "customer_name": field(item, "customer_name")}) == customer
            and normalize_invoice_number(field(item, "invoice_number") or "") == normalize_invoice_number(row["invoice_number"])
            and field(item, "invoice_date") == row["invoice_date"]]
        if prior_same_note:
            periods = sorted({str(field(item, "period") or "earlier period") for item in prior_same_note})
            add_note_issue("NOTE_PREVIOUSLY_PREPARED", f"This note number and date appeared in an earlier GSTR-1 register ({', '.join(periods)}). Check the filed return before reporting it again; earlier preparation does not prove it was filed.")

        reference = row.get("original_invoice_number")
        if not reference:
            links[row_number] = None
            row["note_relationship_status"] = "MISSING_REFERENCE"
            add_note_issue("NOTE_ORIGINAL_MISSING", "No original invoice number was supplied for this credit/debit note.")
            continue
        reference_key = normalize_invoice_number(reference)
        candidates = [item for item in original_groups.values() if item["customer"] == customer and item["number"] == reference_key]
        original_date = row.get("original_invoice_date")
        if original_date:
            candidates = [item for item in candidates if item["date"] == original_date]
        if len(candidates) == 0:
            links[row_number] = None
            row["note_relationship_status"] = "ORIGINAL_NOT_FOUND"
            add_note_issue("NOTE_ORIGINAL_NOT_FOUND", f"Original invoice {reference} could not be matched for this customer. Check its number, date and customer details.")
            continue
        if len(candidates) > 1:
            links[row_number] = None
            row["note_relationship_status"] = "AMBIGUOUS_ORIGINAL"
            add_note_issue("NOTE_ORIGINAL_AMBIGUOUS", f"More than one invoice for this customer matches original reference {reference}. Supply the original invoice date or confirm the correct invoice.")
            continue
        original = candidates[0]
        links[row_number] = original
        row["note_relationship_status"] = "LINKED"
        row["linked_original_invoice"] = {"invoice_number": original["invoice_number"],
            "invoice_date": original["date"].isoformat() if hasattr(original["date"], "isoformat") else str(original["date"]),
            "invoice_value": float(original["invoice_value"]), "taxable_value": float(original["taxable_value"]),
            "tax": float(original["tax"]), "source": original["source"]}
        if original["cancelled"]:
            add_note_issue("NOTE_ORIGINAL_CANCELLED", f"The matched original invoice {original['invoice_number']} is marked cancelled. Confirm the note reference and original transaction before relying on this link.")
        note_key = (original["customer"], original["number"], original["date"], original["period"])
        notes_by_original.setdefault(note_key, []).append(row)

    # Aggregate all note lines against each linked original, including credits
    # from prior period registers, rather than comparing a line to one invoice line.
    for note_key, linked_notes in notes_by_original.items():
        original = links[linked_notes[0]["row_number"]]
        current_credit = sum((row["taxable_value"] for row in linked_notes
            if normalize_document_type(row["document_type"]).startswith("CREDIT_NOTE")), Decimal("0"))
        original_number = original["number"]
        prior_credit = sum((Decimal(str(field(item, "taxable_value") or 0)) for item in historical_notes
            if _customer_key({"customer_gstin": field(item, "customer_gstin"), "customer_name": field(item, "customer_name")}) == original["customer"]
            and normalize_invoice_number(field(item, "original_invoice_number") or "") == original_number), Decimal("0"))
        cumulative_credit = current_credit + prior_credit
        for note in linked_notes:
            if note.get("linked_original_invoice"):
                note["linked_original_invoice"]["cumulative_credit_notes"] = float(cumulative_credit)
                note["linked_original_invoice"]["remaining_taxable_value"] = float(original["taxable_value"] - cumulative_credit)
        if current_credit and cumulative_credit > original["taxable_value"]:
            message = (f"Credit notes linked to original invoice {original['invoice_number']} total ₹{cumulative_credit:,.2f} taxable value, "
                f"while the matched original invoice totals ₹{original['taxable_value']:,.2f}. Check prior adjustments and the remaining amount before reporting.")
            for note in linked_notes:
                if normalize_document_type(note["document_type"]).startswith("CREDIT_NOTE"):
                    row_number = note["row_number"]
                    if not any(issue.get("code") == "NOTE_EXCEEDS_ORIGINAL" for issue in note["issues"]):
                        issue = {"code": "NOTE_EXCEEDS_ORIGINAL", "severity": "REVIEW", "message": message}
                        note["issues"].append(issue)
                        note["status"] = "SYSTEM_FLAGGED"
                        issues.append({"row": row_number, **issue})
    return {row["row_number"]: {"status": row.get("note_relationship_status", "NOT_A_NOTE"),
        "original": row.get("linked_original_invoice"), "reportedStatus": "NOT_CONNECTED_TO_GST_PORTAL"}
        for row in rows if normalize_document_type(row.get("document_type")).startswith(("CREDIT_NOTE", "DEBIT_NOTE"))}


def parse_sales_register(contents: bytes, filename: str, period: str, seller_gstin: str, b2cl_rules: list[dict], rate_rules: list[dict] | None = None,
                         known_profiles: list[dict] | None = None, confirmed_columns: dict[str, str] | None = None,
                         historical_invoices: list[dict] | None = None,
                         business_column_choices: dict[str, str] | None = None) -> dict:
    period_month(period)
    raw_frame, source_type = read_sales_frame(contents, filename)
    if detect_aggregated_sales_frame(raw_frame):
        aggregate_examples = [str(column) for column in raw_frame.columns
            if re.match(r"^(sum|average|avg|count|minimum|maximum) of ",
                re.sub(r"[^a-z0-9]+", " ", str(column).casefold()).strip())]
        summary_examples = [str(column) for column in raw_frame.columns
            if any(token in re.sub(r"[^a-z0-9]+", " ", str(column).casefold())
                for token in ("grand total", "subtotal", "total invoices", "count of invoice"))]
        raise AggregatedSalesDataError(aggregate_examples or summary_examples)
    fingerprint = format_fingerprint(list(raw_frame.columns), source_type)
    matching_profile = next((profile for profile in (known_profiles or []) if profile.get("fingerprint") == fingerprint), None)
    historical_confirmations: dict[str, set[str]] = {}
    for profile in known_profiles or []:
        for header, field in profile.get("confirmed_columns", {}).items():
            historical_confirmations.setdefault(header, set()).add(field)
    learned_rules = {header: next(iter(fields)) for header, fields in historical_confirmations.items() if len(fields) == 1}
    known_mapping = {**learned_rules, **(matching_profile or {}).get("column_rules", {})}
    resolution = resolve_columns(raw_frame, source_type, known_mapping, confirmed_columns, business_column_choices)
    frame = apply_resolution(raw_frame, resolution)
    for col in ("customer_name", "customer_gstin", "invoice_value", "cgst", "sgst", "igst", "cess", "gst_rate", "place_of_supply", "hsn_sac", "description", "document_type", "supply_type", "invoice_series", "original_invoice_number", "invoice_status", "uqc", "export_type", "shipping_bill_number", "port_code", "note_reason"):
        if col not in frame.columns:
            frame[col] = ""
    rows, issues = [], []
    invoice_history = []
    series_rows = {}
    def dec(value, rownum, field, required=False, percentage=False):
        raw = str(value or "").strip()
        if not raw and not required:
            return Decimal("0")
        try:
            return normalize_percentage(raw) if percentage else normalize_money(raw)
        except ValueError:
            issues.append({"row": rownum, "code": "INVALID_AMOUNT", "severity": "ERROR", "message": f"{field} is not a valid numeric amount."})
            return Decimal("0")
    for index, raw in frame.iterrows():
        rownum = int(index) + 2
        initial_issue_count = len(issues)
        invoice = str(raw.get("invoice_number", "")).strip()
        try:
            raw_date = str(raw.get("invoice_date", "")).strip()
            # ISO dates are year-first regardless of locale. Keep day-first
            # parsing for common spreadsheet dates such as 05/09/2026.
            if re.fullmatch(r"\d+(?:\.0)?", raw_date) and float(raw_date) > 20000:
                inv_date = (pd.Timestamp("1899-12-30") + pd.to_timedelta(float(raw_date), unit="D")).date()
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", raw_date):
                inv_date = date.fromisoformat(raw_date[:10])
            else:
                inv_date = pd.to_datetime(raw_date, dayfirst=True, errors="raise").date()
        except Exception:
            issues.append({"row": rownum, "code": "INVALID_DATE", "severity": "ERROR", "message": "Invoice date could not be parsed."})
            continue
        if not invoice:
            issues.append({"row": rownum, "code": "MISSING_INVOICE_NUMBER", "severity": "ERROR", "message": "Invoice number is required."})
            continue
        customer_gstin = normalize_gstin(str(raw.get("customer_gstin", ""))) or None
        taxable = dec(raw.get("taxable_value"), rownum, "Taxable amount", True)
        cgst, sgst, igst, cess = (dec(raw.get(k), rownum, label) for k, label in (("cgst", "CGST"), ("sgst", "SGST"), ("igst", "IGST"), ("cess", "cess")))
        calculated_total = (taxable + cgst + sgst + igst + cess).quantize(CENT)
        supplied_total = dec(raw.get("invoice_value"), rownum, "Invoice total")
        total = supplied_total or calculated_total
        rate_raw = str(raw.get("gst_rate", "")).strip()
        rate = dec(rate_raw, rownum, "GST rate", percentage=True) if rate_raw else None
        try:
            quantity = normalize_quantity(raw.get("quantity"))
        except ValueError:
            issues.append({"row": rownum, "code": "INVALID_QUANTITY", "severity": "ERROR", "message": "Quantity is not a valid number."})
            quantity = Decimal("0")
        raw_pos = str(raw.get("place_of_supply", "")).strip()
        pos = normalize_place_of_supply(raw_pos)
        raw_doc_type = str(raw.get("document_type", "INVOICE")).strip()
        raw_supply_type = str(raw.get("supply_type", "")).strip()
        doc_type, raw_supply_type, source_category = _separate_source_document_and_category(raw_doc_type, raw_supply_type)
        if not raw_supply_type:
            raw_doc_tokens = re.sub(r"[^A-Z0-9]+", " ", raw_doc_type.upper())
            if "BRANCH" in raw_doc_tokens and ("TRANSFER" in raw_doc_tokens or "TRANSF" in raw_doc_tokens):
                raw_supply_type = "BRANCH TRANSFER"
            elif "EMPLOYEE" in raw_doc_tokens and ("REIMB" in raw_doc_tokens or "CATERER" in raw_doc_tokens):
                raw_supply_type = "EMPLOYEE REIMBURSEMENT"
            elif "DUTY SCRIPT" in raw_doc_tokens or "EXEMPT" in raw_doc_tokens or "NIL RATED" in raw_doc_tokens:
                raw_supply_type = "EXEMPT"
            elif "EXPORT SERVICE" in raw_doc_tokens:
                raw_supply_type = "EXPORT SERVICE"
            elif "EXPORT" in raw_doc_tokens:
                raw_supply_type = "EXPORT"
            elif "SEZ" in raw_doc_tokens:
                raw_supply_type = "SEZ"
        supply_type = normalize_supply_type(raw_supply_type)
        if doc_type.startswith(("CREDIT_NOTE", "DEBIT_NOTE")):
            # Note ledgers may store adjustment values as negative while others
            # store them positive. Keep normalized source values positive here;
            # the annexure applies the credit/debit sign consistently.
            taxable = taxable.copy_abs()
            cgst, sgst, igst, cess = (value.copy_abs() for value in (cgst, sgst, igst, cess))
            supplied_total = supplied_total.copy_abs()
            calculated_total = (taxable + cgst + sgst + igst + cess).quantize(CENT)
            total = supplied_total or calculated_total
        invoice_issues = []
        for diagnostic in (item for item in resolution["row_diagnostics"] if item["row_number"] == rownum):
            invoice_issues.append({"code": diagnostic["code"], "severity": "REVIEW",
                "message": f"A duplicate {diagnostic['field']} source column contains an unrecognized value ({diagnostic['raw_value']!r}). The valid value from another equivalent column was used; check the original row."})
        original_invoice = str(raw.get("original_invoice_number", "")).strip() or None
        original_date_raw = str(raw.get("original_invoice_date", "")).strip()
        original_invoice_date = None
        if original_date_raw:
            try:
                original_invoice_date = (date.fromisoformat(original_date_raw[:10]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", original_date_raw)
                    else pd.to_datetime(original_date_raw, dayfirst=True, errors="raise").date())
            except Exception:
                invoice_issues.append({"code": "ORIGINAL_INVOICE_DATE_REVIEW", "severity": "REVIEW", "message": "The original invoice date on this note could not be read. Check it against the source document."})
        shipping_date_raw = str(raw.get("shipping_bill_date", "")).strip()
        shipping_bill_date = None
        if shipping_date_raw:
            try:
                shipping_bill_date = (date.fromisoformat(shipping_date_raw[:10]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", shipping_date_raw)
                    else pd.to_datetime(shipping_date_raw, dayfirst=True, errors="raise").date())
            except Exception:
                invoice_issues.append({"code": "SHIPPING_BILL_DATE_REVIEW", "severity": "REVIEW", "message": "The shipping bill date could not be read. Check it against the export documents."})
        invoice_status = str(raw.get("invoice_status", "ACTIVE")).strip().upper() or "ACTIVE"
        # A malformed numeric cell must never become a zero-valued imported invoice.
        if any(issue.get("row") == rownum and issue.get("severity") == "ERROR" for issue in issues[initial_issue_count:]):
            continue
        if taxable <= 0:
            issues.append({"row": rownum, "code": "NON_POSITIVE_TAXABLE_VALUE", "severity": "ERROR", "message": "Taxable value must be greater than zero."})
            continue
        classification, reason, _rule = classify_invoice(seller_gstin=seller_gstin, customer_gstin=customer_gstin,
            place_of_supply=pos, invoice_value=total, period=period, b2cl_rules=b2cl_rules, document_type=doc_type,
            supply_type=supply_type)
        normalized = normalize_invoice_number(invoice)
        explicit_series = str(raw.get("invoice_series", "")).strip().upper()
        match = re.search(r"^(.*?)(\d+)$", normalized)
        series = explicit_series or (match.group(1) if match else "UNNUMBERED")
        if match:
            series_rows.setdefault(series, []).append((int(match.group(2)), rownum, invoice))
        customer_name = str(raw.get("customer_name", "")).strip()
        customer_name_key = re.sub(r"[^A-Za-z0-9]+", "", customer_name).upper()
        customer_key = customer_gstin or customer_name_key or f"UNIDENTIFIED_ROW_{rownum}"
        if source_category in {"B2B", "B2C"}:
            expected_bucket = source_category
            actual_bucket = "B2B" if classification == "B2B" else "B2C" if classification in {"B2CL", "B2CS"} else None
            if actual_bucket and actual_bucket != expected_bucket:
                invoice_issues.append({"code": "SOURCE_CATEGORY_DIFFERS", "severity": "REVIEW",
                    "message": f"The source labels this row {expected_bucket}, while recipient and supply details classify it as {actual_bucket}. Check the source invoice and GSTIN before filing."})
        if doc_type == "OTHER":
            invoice_issues.append({"code": "DOCUMENT_TYPE_REQUIRES_REVIEW", "severity": "REVIEW",
                "message": f"Source value '{raw_doc_type or 'not supplied'}' is not a recognized document kind. Confirm whether this is an Invoice, Credit Note or Debit Note; B2B/B2C/export labels describe the supply category."})
        if raw_pos and not pos:
            invoice_issues.append({"code": "PLACE_OF_SUPPLY_UNRECOGNIZED", "severity": "REVIEW", "message": f"Place of supply '{raw_pos}' could not be mapped to a GST state code. Confirm the supplied state."})
        if customer_gstin:
            valid_customer_gstin, gstin_message = validate_gstin(customer_gstin)
            if not valid_customer_gstin:
                invoice_issues.append({"code": "GSTIN_INVALID", "severity": "REVIEW", "message": f"Recipient GSTIN requires review: {gstin_message}. The transaction is left unclassified until corrected."})
            elif pos and customer_gstin[:2] != pos:
                invoice_issues.append({"code": "RECIPIENT_GSTIN_POS_MISMATCH", "severity": "REVIEW", "message": f"Place of supply state {pos} differs from recipient GSTIN state {customer_gstin[:2]}; confirm the recipient and place-of-supply details."})
        if invoice_status not in {"ACTIVE", "CANCELLED", "CANCELED", "VOID"}:
            invoice_issues.append({"code": "INVOICE_STATUS_UNRECOGNIZED", "severity": "REVIEW", "message": f"Source invoice status '{invoice_status}' is not recognized. Confirm whether this document is active or cancelled."})
        if invoice_status in {"CANCELLED", "CANCELED", "VOID"}:
            invoice_issues.append({"code": "CANCELLED_INVOICE_INCLUDED", "severity": "REVIEW", "message": "This source row is marked cancelled but is included in the GSTR-1 preparation. Confirm whether it should be reported."})
        current_tax = cgst + sgst + igst + cess
        raw_number_key = re.sub(r"\s+", "", invoice).upper()
        matching_identity = [prior for prior in invoice_history if prior["customer"] == customer_key and prior["document_type"] == doc_type]
        matching_numbers = [prior for prior in matching_identity if prior["normalized"] == normalized]
        duplicate_match = next((prior for prior in matching_numbers if prior["invoice_date"] == inv_date and prior["taxable"] == taxable and prior["tax"] == current_tax), None)
        # A repeated invoice number and date commonly represents product lines
        # from one invoice. Treat changed line amounts as separate lines; a
        # reused number is review-worthy when its date changes. Exact matches
        # on all duplicate signals are still flagged above.
        matching_number = next((prior for prior in matching_numbers if prior["invoice_date"] != inv_date), None)
        if duplicate_match:
            if duplicate_match["raw_number"] == raw_number_key:
                duplicate_code = "POSSIBLE_DUPLICATE_NOTE" if doc_type.startswith(("CREDIT_NOTE", "DEBIT_NOTE")) else "POSSIBLE_DUPLICATE"
                similarity_text = "100% same invoice number"
            else:
                duplicate_code = "POSSIBLE_DUPLICATE_NOTE" if doc_type.startswith(("CREDIT_NOTE", "DEBIT_NOTE")) else "POSSIBLE_FUZZY_DUPLICATE"
                similarity = SequenceMatcher(None, duplicate_match["raw_number"], raw_number_key).ratio()
                similarity_text = f"{similarity:.0%} invoice-number similarity"
        elif matching_number:
            issue = {"code": "INVOICE_NUMBER_REUSED", "severity": "REVIEW",
                "message": f"Invoice number matches row {matching_number['row']} for this customer, but the invoice date differs. Confirm whether this is a legitimate number reuse or a data error."}
            invoice_issues.append(issue)
            issues.append({"row": matching_number["row"], **issue})
            prior_row = next((item for item in rows if item["row_number"] == matching_number["row"]), None)
            if prior_row is not None:
                prior_row["issues"].append(issue)
                prior_row["status"] = "SYSTEM_FLAGGED"
        else:
            for prior in matching_identity:
                if prior["invoice_date"] != inv_date or prior["taxable"] != taxable or prior["tax"] != current_tax:
                    continue
                similarity = SequenceMatcher(None, prior["raw_number"], raw_number_key).ratio()
                if similarity >= 0.80:
                    duplicate_match = prior
                    duplicate_code = "POSSIBLE_DUPLICATE_NOTE" if doc_type.startswith(("CREDIT_NOTE", "DEBIT_NOTE")) else "POSSIBLE_FUZZY_DUPLICATE"
                    similarity_text = f"{similarity:.0%} invoice-number similarity"
                    break
        if duplicate_match:
            repeated_label = "credit/debit note" if doc_type.startswith(("CREDIT_NOTE", "DEBIT_NOTE")) else "invoice"
            message = (f"Possible duplicate {repeated_label} at row {duplicate_match['row']}: same customer, document date, taxable value and total tax, with {similarity_text}. "
                       "Both rows are retained; check whether these are duplicated documents or separate line items.")
            invoice_issues.append({"code": duplicate_code, "severity": "REVIEW", "message": message})
            prior_row = next((item for item in rows if item["row_number"] == duplicate_match["row"]), None)
            if prior_row is not None:
                prior_row["issues"].append({"code": duplicate_code, "severity": "REVIEW", "message": f"Possible duplicate {repeated_label} at row {rownum}: same customer, document date, taxable value and total tax, with {similarity_text}. Both rows are retained; check whether these are duplicated documents or separate line items."})
                prior_row["status"] = "SYSTEM_FLAGGED"
            issues.append({"row": duplicate_match["row"], "code": duplicate_code, "severity": "REVIEW",
                "message": f"Possible duplicate {repeated_label} at row {rownum}: same customer, document date, taxable value and total tax, with {similarity_text}. Both rows are retained; check whether these are duplicated documents or separate line items."})
        invoice_history.append({"customer": customer_key, "normalized": normalized, "raw_number": raw_number_key,
            "invoice_date": inv_date, "taxable": taxable, "tax": current_tax, "row": rownum, "document_type": doc_type})
        seller_state = (seller_gstin or "")[:2]
        reported_gst = cgst + sgst + igst
        if igst > 0 and (cgst > 0 or sgst > 0) and not (pos and seller_state):
            invoice_issues.append({"code": "TAX_SPLIT_CONFLICT", "severity": "REVIEW",
                "message": f"IGST ₹{igst:,.2f} and local tax components (CGST ₹{cgst:,.2f}, SGST ₹{sgst:,.2f}) are both reported. Confirm the place of supply and tax treatment."})
        if pos and seller_state and reported_gst > 0:
            intra_state = pos == seller_state
            issues_with_split = []
            if intra_state:
                if igst > 0:
                    issues_with_split.append("IGST is reported for a same-state supply")
                if (cgst > 0) != (sgst > 0):
                    issues_with_split.append("only one of CGST or SGST is reported")
                elif cgst > 0 and abs(cgst - sgst) > CENT:
                    issues_with_split.append("CGST and SGST amounts differ by more than ₹0.01")
            else:
                if igst <= 0:
                    issues_with_split.append("IGST is missing for a different-state supply")
                if cgst > 0 or sgst > 0:
                    issues_with_split.append("CGST or SGST is also reported")
            if issues_with_split:
                tax_treatment = "intra-state (CGST + SGST; normally no IGST)" if intra_state else "inter-state (IGST; normally no CGST/SGST)"
                invoice_issues.append({"code": "PLACE_OF_SUPPLY_TAX_SPLIT", "severity": "REVIEW",
                    "message": f"Tax split needs review: seller state {seller_state} and place-of-supply state {pos} suggest {tax_treatment}. Reported CGST ₹{cgst:,.2f}, SGST ₹{sgst:,.2f}, IGST ₹{igst:,.2f} (GST total ₹{reported_gst:,.2f}); {', '.join(issues_with_split)}. Confirm the transaction facts and applicable treatment; no tax amounts were changed."})
        elif igst > 0 and (cgst > 0 or sgst > 0):
            invoice_issues.append({"code": "TAX_SPLIT_CONFLICT", "severity": "REVIEW",
                "message": f"IGST ₹{igst:,.2f} and local tax components (CGST ₹{cgst:,.2f}, SGST ₹{sgst:,.2f}) are both reported. Confirm the place of supply and tax treatment."})
        if rate is not None:
            expected_tax = (taxable * rate / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
            reported_tax = cgst + sgst + igst
            difference = abs(expected_tax - reported_tax)
            if difference > CENT:
                invoice_issues.append({"code": "TAX_RATE_ARITHMETIC", "severity": "REVIEW",
                    "message": f"Tax amount mismatch using the invoice's entered rate: taxable value ₹{taxable:,.2f} × {rate.normalize():f}% gives expected GST ₹{expected_tax:,.2f}; reported CGST+SGST+IGST is ₹{reported_tax:,.2f}; difference ₹{difference:,.2f}. This checks arithmetic only and does not determine whether the entered rate is legally applicable."})
        applicable_rate_rules = []
        for candidate in rate_rules or []:
            effective_from = date.fromisoformat(candidate["effective_from"])
            effective_to = date.fromisoformat(candidate["effective_to"]) if candidate.get("effective_to") else None
            if effective_from <= inv_date and (effective_to is None or inv_date <= effective_to):
                applicable_rate_rules.append(candidate)
        applicable_rate_rules.sort(key=lambda item: (item.get("effective_from", ""), item.get("scope") == "firm"), reverse=True)
        hsn_code = str(raw.get("hsn_sac", "")).strip()
        if re.fullmatch(r"\d+\.0", hsn_code):
            hsn_code = hsn_code[:-2]
        hsn_code = re.sub(r"\s+", "", hsn_code)
        if applicable_rate_rules and hsn_code:
            rate_rule = next((candidate for candidate in applicable_rate_rules
                if _reference_rates_for_hsn(candidate.get("definition", {}), hsn_code)), applicable_rate_rules[0])
            reference_rates = _reference_rates_for_hsn(rate_rule.get("definition", {}), hsn_code)
            source = f"; source {rate_rule['source_url']}" if rate_rule.get("source_url") else ""
            verified = f"; verified {rate_rule['verified_at'][:10]}" if rate_rule.get("verified_at") else ""
            effective_range = f"effective {rate_rule['effective_from']}" + (f" through {rate_rule['effective_to']}" if rate_rule.get("effective_to") else " onward")
            if not reference_rates:
                invoice_issues.append({"code": "GST_RATE_REFERENCE_NOT_FOUND", "severity": "REVIEW",
                    "message": f"No configured rate entry was found for HSN/SAC {hsn_code} in reference {rate_rule['version']} ({effective_range}{verified}{source}). Confirm classification and rate treatment; no rate conclusion was made."})
            else:
                reference_labels = ", ".join(f"{item.normalize():f}%" for item in reference_rates)
                if rate is None:
                    invoice_issues.append({"code": "GST_RATE_MISSING", "severity": "REVIEW",
                        "message": f"GST rate requires review: no invoice rate was supplied for HSN/SAC {hsn_code}. Configured reference rate option(s): {reference_labels} ({rate_rule['version']}, {effective_range}{verified}{source}). Confirm the applicable treatment; reference data is not a definitive rate determination."})
                elif rate not in reference_rates:
                    expected_amounts = ", ".join(money for money in (f"₹{(taxable * item / Decimal('100')).quantize(CENT, rounding=ROUND_HALF_UP):,.2f}" for item in reference_rates))
                    reported_tax = cgst + sgst + igst
                    invoice_rate_text = f"{rate.normalize():f}%"
                    message = (f"GST rate requires review for HSN/SAC {hsn_code}: invoice rate {invoice_rate_text}; configured reference rate option(s) {reference_labels} "
                        f"({rate_rule['version']}, {effective_range}{verified}{source}). On taxable value ₹{taxable:,.2f}, reference-rate GST amount(s) would be {expected_amounts}; reported CGST+SGST+IGST is ₹{reported_tax:,.2f}. "
                        "This is a review prompt, not a definitive rate determination; confirm classification, exemptions, notifications and supply facts.")
                    invoice_issues.append({"code": "GST_RATE_REFERENCE_MISMATCH", "severity": "REVIEW", "message": message})
        if inv_date.strftime("%Y-%m") != period:
            invoice_issues.append({"code": "PERIOD_MISMATCH", "severity": "REVIEW", "message": "Invoice date falls outside the selected tax period."})
        if not hsn_code:
            invoice_issues.append({"code": "HSN_MISSING", "severity": "REVIEW", "message": "HSN/SAC is missing; applicability and required digits need review."})
        elif not re.fullmatch(r"[A-Za-z0-9]{4,8}", hsn_code):
            invoice_issues.append({"code": "HSN_FORMAT_REVIEW", "severity": "REVIEW", "message": "HSN/SAC must contain 4 to 8 letters or digits for this format check. Confirm the code and applicable reporting requirement."})
        for issue in invoice_issues:
            issues.append({"row": rownum, **issue})
        rows.append({"row_number": rownum, "customer_name": customer_name or None,
            "customer_gstin": customer_gstin, "invoice_number": invoice, "normalized_invoice_number": normalized,
            "document_series": series,
            "invoice_date": inv_date, "invoice_value": total, "taxable_value": taxable, "cgst": cgst,
            "sgst": sgst, "igst": igst, "cess": cess, "gst_rate": rate, "place_of_supply": pos,
            "hsn_sac": hsn_code or None,
            "description": str(raw.get("description", "")).strip() or None,
            "original_invoice_number": original_invoice, "invoice_status": invoice_status,
            "original_invoice_date": original_invoice_date, "note_reason": str(raw.get("note_reason", "")).strip() or None,
            "quantity": quantity, "uqc": str(raw.get("uqc", "")).strip() or None,
            "export_type": str(raw.get("export_type", "")).strip() or None,
            "shipping_bill_number": str(raw.get("shipping_bill_number", "")).strip() or None,
            "shipping_bill_date": shipping_bill_date, "port_code": str(raw.get("port_code", "")).strip() or None,
            "supply_type": supply_type,
            "document_type": doc_type, "classification": classification,
            "classification_reason": reason, "status": "SYSTEM_FLAGGED" if invoice_issues else "NEEDS_REVIEW",
            "issues": invoice_issues})
    note_relationships = validate_note_relationships(rows, issues, historical_invoices, period)
    # Compare totals at invoice level: exports often repeat one invoice total on
    # every product line. Sum the line amounts and count a repeated invoice
    # total once, instead of producing a false warning on every line.
    invoice_groups = {}
    for row in rows:
        recipient = row.get("customer_gstin") or re.sub(r"[^a-z0-9]+", "", (row.get("customer_name") or "").casefold())
        invoice_groups.setdefault((recipient, row["normalized_invoice_number"], row["invoice_date"]), []).append(row)
    for grouped_rows in invoice_groups.values():
        calculated_total = sum((row["taxable_value"] + row["cgst"] + row["sgst"] + row["igst"] + row["cess"]
            for row in grouped_rows), Decimal("0.00")).quantize(CENT)
        source_totals = [row["invoice_value"] for row in grouped_rows]
        repeated_header_total = len(source_totals) > 1 and len(set(source_totals)) == 1
        summed_source_total = sum(source_totals, Decimal("0.00"))
        if repeated_header_total:
            # Prefer the sum when line totals tie to the combined calculation;
            # otherwise treat the repeated value as one copied invoice total.
            source_total = (summed_source_total if abs(summed_source_total - calculated_total) <= Decimal("1.00")
                else source_totals[0])
        else:
            source_total = summed_source_total
        difference = abs(source_total - calculated_total)
        if difference > Decimal("1.00"):
            detail = ("Combined product lines were checked and a repeated invoice total was counted once. "
                if len(grouped_rows) > 1 else "Compare the source total with taxable value, tax and any extra charges. ")
            issue = {"code": "TOTAL_MISMATCH", "severity": "REVIEW",
                "message": f"Invoice value differs from taxable value plus tax by ₹{difference:,.2f}. {detail}"}
            issues.append({"row": grouped_rows[0]["row_number"], **issue})
            grouped_rows[0]["issues"].append(issue)
            grouped_rows[0]["status"] = "SYSTEM_FLAGGED"

    customers_by_name = {}
    for row in rows:
        customer_name_key = re.sub(r"[^a-z0-9]+", "", (row.get("customer_name") or "").casefold())
        if customer_name_key and row.get("customer_gstin"):
            customers_by_name.setdefault(customer_name_key, set()).add(row["customer_gstin"])
    inconsistent_names = {name for name, gstins in customers_by_name.items() if len(gstins) > 1}
    for row in rows:
        customer_name_key = re.sub(r"[^a-z0-9]+", "", (row.get("customer_name") or "").casefold())
        if customer_name_key in inconsistent_names:
            issue = {"row": row["row_number"], "code": "CUSTOMER_GSTIN_INCONSISTENT", "severity": "REVIEW",
                "message": "The same customer name is linked to different GSTINs within this register. Check the customer master and recipient details."}
            issues.append(issue)
            row["issues"].append({k: v for k, v in issue.items() if k != "row"})
            row["status"] = "SYSTEM_FLAGGED"
    row_by_number = {row["row_number"]: row for row in rows}
    for series, entries in series_rows.items():
        ordered = sorted(entries)
        seen_numbers = set()
        for number, rownum, _invoice in ordered:
            if number in seen_numbers:
                continue
            seen_numbers.add(number)
        unique_numbers = sorted(seen_numbers)
        for previous, current in zip(unique_numbers, unique_numbers[1:]):
            if current - previous > 1:
                target = next(entry for entry in ordered if entry[0] == current)
                message = (f"Possible gap in invoice series '{series}': numbers {previous + 1} through "
                           f"{current - 1} are not present in this upload. This may be a cancellation, "
                           "another series, or an incomplete import; verify before drawing a conclusion.")
                issues.append({"row": target[1], "code": "INVOICE_SEQUENCE_GAP", "severity": "REVIEW", "message": message})
                if target[1] in row_by_number:
                    row_by_number[target[1]]["issues"].append({"code": "INVOICE_SEQUENCE_GAP", "severity": "REVIEW", "message": message})
                    row_by_number[target[1]]["status"] = "SYSTEM_FLAGGED"
    _, active_threshold_version = b2cl_threshold(period, b2cl_rules)
    invoice_dates = {row["invoice_date"].isoformat() for row in rows}
    active_rate_reference_rules = []
    for rule in rate_rules or []:
        start = date.fromisoformat(rule["effective_from"])
        end = date.fromisoformat(rule["effective_to"]) if rule.get("effective_to") else None
        if any(start <= date.fromisoformat(invoice_date) and (end is None or date.fromisoformat(invoice_date) <= end) for invoice_date in invoice_dates):
            active_rate_reference_rules.append(rule)
    rate_reference_snapshot = {"applicable": bool(active_rate_reference_rules),
        "rule_versions": sorted({rule["version"] for rule in active_rate_reference_rules}),
        "rules": active_rate_reference_rules,
        "detail": "Effective-dated reference data was available at import. Differences remain review prompts." if active_rate_reference_rules
            else "No effective-dated reference data was available when this register was imported. Add a reference and re-import to run the comparison."}
    return {"rows": rows, "issues": issues, "note_relationships": note_relationships,
        "source_sha256": hashlib.sha256(contents).hexdigest(), "rule_version": active_threshold_version,
        "rate_reference_snapshot": rate_reference_snapshot,
        "_format_profile": {"source_type": source_type, "fingerprint": resolution["fingerprint"],
            "source_columns": [str(column) for column in raw_frame.columns], "column_rules": resolution["mapping"],
            "confirmed_columns": resolution["confirmed_columns"], "alias_version": resolution["alias_version"],
            "resolution_decisions": resolution["resolution_decisions"], "row_diagnostics": resolution["row_diagnostics"]}}
