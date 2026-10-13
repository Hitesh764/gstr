"""Business-facing, transaction-level GSTR-1 annexure views.

The service consumes normalized GSTR1SalesInvoice records only. Source file
headers and mapping metadata never enter report output.
"""
from collections import defaultdict
from decimal import Decimal

from app.ingestion.gstr1_sales import normalize_document_type, normalize_supply_type, reporting_category


# Version the presentation mapping so historical annexures can be regenerated
# consistently when GSTR-1 table conventions change. Threshold selection for
# B2C Large/Others remains in the effective-dated return rules.
TABLE_MAPS = (
    {"version": "GSTR1_TABLE_MAP_2025_05", "effective_from": "2025-05", "effective_to": None,
     "tables": {"B2B": "Table 4A - B2B supplies", "B2CL": "Table 5 - B2C large",
        "B2CS": "Table 7 - B2C others", "EXPORT": "Table 6A - Exports",
        "SEZ": "Table 6B/6C - SEZ supplies", "DEEMED_EXPORT": "Table 6 - Deemed exports",
        "EXEMPT": "Table 8 - Nil/exempt supplies", "CDNR": "Table 9B - Registered recipients",
        "CDNUR": "Table 9B - Unregistered recipients"}},
    {"version": "GSTR1_TABLE_MAP_1", "effective_from": "1900-01", "effective_to": "2025-04",
     "tables": {"B2B": "Table 4A - B2B supplies", "B2CL": "Table 5 - B2C large",
        "B2CS": "Table 7 - B2C others", "EXPORT": "Table 6A - Exports",
        "SEZ": "Table 6B/6C - SEZ supplies", "DEEMED_EXPORT": "Table 6 - Deemed exports",
        "EXEMPT": "Table 8 - Nil/exempt supplies", "CDNR": "Table 9B - Registered recipients",
        "CDNUR": "Table 9B - Unregistered recipients"}},
)


def table_map_for_period(period: str) -> dict:
    return next((item for item in TABLE_MAPS if item["effective_from"] <= period
        and (item["effective_to"] is None or period <= item["effective_to"])), TABLE_MAPS[0])


def destination_table(document_type: str, category: str, has_gstin: bool, period: str = "2025-05") -> str:
    """Return a period-versioned GSTR-1 preparation table label."""
    tables = table_map_for_period(period)["tables"]
    doc = normalize_document_type(document_type)
    if doc.startswith(("CREDIT_NOTE", "DEBIT_NOTE")):
        return tables["CDNR"] if has_gstin else tables["CDNUR"]
    return tables.get(category, "Needs classification")


def transaction_view(row, issues, seller_gstin: str, period: str, b2cl_rules: list[dict]) -> dict:
    category = reporting_category(row.document_type, row.supply_type, row.classification,
        seller_gstin=seller_gstin, customer_gstin=row.customer_gstin, place_of_supply=row.place_of_supply,
        invoice_value=row.invoice_value, period=period, b2cl_rules=b2cl_rules)
    active_issues = [issue for issue in issues if issue.status == "OPEN"]
    if any(issue.severity == "ERROR" for issue in active_issues):
        status = "ERROR"
    elif active_issues:
        status = "REVIEW_REQUIRED"
    else:
        status = "READY"
    doc_type = normalize_document_type(row.document_type)
    if doc_type.startswith("CREDIT_NOTE"):
        document_label = "Credit note"
    elif doc_type.startswith("DEBIT_NOTE"):
        document_label = "Debit note"
    elif doc_type == "INVOICE":
        document_label = "Invoice"
    else:
        document_label = doc_type.replace("_", " ").title()
    category_labels = {"B2B": "B2B", "B2CL": "B2C Large", "B2CS": "B2C Others", "EXPORT": "Export",
        "SEZ": "SEZ supply", "DEEMED_EXPORT": "Deemed export", "EXEMPT": "Nil/exempt", "CREDIT_NOTE": "Credit note",
        "DEBIT_NOTE": "Debit note", "UNCLASSIFIED": "Needs classification", "OTHER": "Needs classification"}
    return {
        "id": row.id,
        "invoiceNumber": row.invoice_number,
        "invoiceDate": row.invoice_date.isoformat(),
        "customerName": row.customer_name,
        "customerGstin": row.customer_gstin,
        "placeOfSupply": row.place_of_supply,
        "supplyType": normalize_supply_type(row.supply_type).replace("_", " ").title(),
        "documentType": document_label,
        "taxableValue": float(row.taxable_value),
        "gstRate": float(row.gst_rate) if row.gst_rate is not None else None,
        "cgst": float(row.cgst), "sgst": float(row.sgst), "igst": float(row.igst), "cess": float(row.cess),
        "invoiceValue": float(row.invoice_value), "hsnSac": row.hsn_sac, "description": row.description,
        "quantity": float(row.quantity or 0), "uqc": row.uqc, "exportType": row.export_type,
        "shippingBillNumber": row.shipping_bill_number,
        "shippingBillDate": row.shipping_bill_date.isoformat() if row.shipping_bill_date else None,
        "portCode": row.port_code, "noteReason": row.note_reason,
        "originalInvoiceNumber": row.original_invoice_number,
        "originalInvoiceDate": row.original_invoice_date.isoformat() if row.original_invoice_date else None,
        "classification": category_labels.get(category, category.replace("_", " ").title()),
        "gstr1Table": destination_table(row.document_type, category, bool(row.customer_gstin), period),
        "tableMappingVersion": table_map_for_period(period)["version"],
        "validationStatus": status,
        "classificationReason": row.classification_reason,
        "exceptions": [{"title": _issue_title(issue.code), "message": issue.message, "severity": issue.severity,
                        "status": issue.status, "id": issue.id} for issue in issues],
    }


def _issue_title(code: str) -> str:
    labels = {
        "POSSIBLE_DUPLICATE": "Possible duplicate invoice", "POSSIBLE_FUZZY_DUPLICATE": "Invoice number resembles another",
        "INVOICE_NUMBER_REUSED": "Invoice number used with different details", "GSTIN_INVALID": "Recipient GSTIN needs checking",
        "TOTAL_MISMATCH": "Invoice total does not add up", "TAX_RATE_ARITHMETIC": "Tax amount differs from the entered rate",
        "TAX_SPLIT_CONFLICT": "Tax components need checking", "PLACE_OF_SUPPLY_TAX_SPLIT": "Tax split and place of supply differ",
        "HSN_MISSING": "HSN/SAC is missing", "HSN_FORMAT_REVIEW": "HSN/SAC format needs checking",
        "DOCUMENT_TYPE_REQUIRES_REVIEW": "Document type needs checking", "NOTE_ORIGINAL_MISSING": "Original invoice reference is missing",
        "NOTE_ORIGINAL_NOT_FOUND": "Original invoice was not found", "NOTE_ORIGINAL_AMBIGUOUS": "Original invoice reference is ambiguous",
        "NOTE_ORIGINAL_CANCELLED": "Original invoice is cancelled", "NOTE_EXCEEDS_ORIGINAL": "Credit notes exceed original taxable value",
        "NOTE_PREVIOUSLY_PREPARED": "Note appeared in an earlier register", "NOTE_PORTAL_REPORTING_CHECK": "Check note against filed returns",
        "POSSIBLE_DUPLICATE_NOTE": "Possible duplicate credit/debit note", "PERIOD_MISMATCH": "Invoice date is outside this period",
    }
    return labels.get(code, code.replace("_", " ").title())


def build_hsn_summary(rows: list[dict]) -> list[dict]:
    groups = defaultdict(lambda: {"hsnSac": None, "description": None, "uqc": None, "gstRate": None,
        "reportingGroup": None, "documents": 0, "records": 0, "_documentKeys": set(),
        "quantity": Decimal("0"), "taxableValue": Decimal("0"), "cgst": Decimal("0"), "sgst": Decimal("0"), "igst": Decimal("0"), "cess": Decimal("0")})
    for row in rows:
        reporting_group = row.get("reportingGroup") or row.get("reporting_group") or "All categories"
        key = (row.get("hsnSac") or row.get("hsn_sac") or "Missing",
            row.get("description") or "", row.get("uqc") or "",
            row.get("gstRate", row.get("gst_rate")), reporting_group)
        group = groups[key]
        group["hsnSac"], group["description"], group["uqc"], group["gstRate"], group["reportingGroup"] = key
        group["records"] += 1
        document_type = row.get("documentType") or row.get("document_type") or "Invoice"
        sign = Decimal("-1") if str(document_type).replace("_", " ").upper().startswith("CREDIT NOTE") else Decimal("1")
        document_key = (row.get("customerGstin") or row.get("customer_gstin"),
            row.get("invoiceNumber") or row.get("invoice_number") or row.get("id"), document_type)
        group["_documentKeys"].add(document_key)
        for field, source in (("quantity", "quantity"), ("taxableValue", "taxableValue"),
                ("cgst", "cgst"), ("sgst", "sgst"), ("igst", "igst"), ("cess", "cess")):
            value = row.get(source, row.get({"taxableValue":"taxable_value"}.get(source, source))) or 0
            group[field] += Decimal(str(value)) * sign
    result = []
    for _, group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][4], item[0][3] or 0, item[0][1], item[0][2])):
        group["documents"] = len(group.pop("_documentKeys"))
        result.append({**group, **{field: float(group[field]) for field in ("quantity", "taxableValue", "cgst", "sgst", "igst", "cess")}})
    return result
