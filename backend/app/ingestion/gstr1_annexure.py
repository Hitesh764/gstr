"""GSTR-1 accountant annexure turnover rollup.

The export/SEZ and export-IGST columns are supplementary breakouts included
within the main taxable and tax columns; they must not be added twice.
"""
from collections import OrderedDict
from decimal import Decimal

from app.ingestion.gstr1_sales import normalize_document_type, normalize_supply_type
from app.validation import validate_gstin

ANNEXURE_LINES = OrderedDict([
    ("b2b_sales", "B2B Sales"),
    ("b2b_branch_transfer", "B2B Sales - Branch Transfer"),
    ("b2b_sez_sales", "B2B (SEZ) Sales"),
    ("b2c_small", "B2C Sales - Small"),
    ("b2c_large", "B2C Sales - Large"),
    ("employee_reimbursement", "Employee Reimbursement - B2C"),
    ("exempt_sales", "Exempt / Nil Rated Sales"),
    ("export_sales", "Export Sales"),
    ("deemed_export_sales", "Deemed Export Sales"),
    ("credit_b2b", "Credit Notes - B2B"),
    ("credit_b2c", "Credit Notes - B2C"),
    ("credit_export", "Credit Notes - Export"),
    ("credit_sez", "Credit Notes - SEZ"),
    ("credit_export_service", "Credit Notes - Export Service"),
    ("credit_b2b_financial", "Credit Note B2B (Financial)"),
    ("credit_b2c_financial", "Credit Note B2C (Financial)"),
    ("debit_b2b", "Debit Notes - B2B"),
    ("debit_b2c", "Debit Notes - B2C"),
    ("debit_export", "Debit Notes - Export"),
    ("debit_sez", "Debit Notes - SEZ"),
    ("debit_export_service", "Debit Notes - Export Service"),
    ("debit_b2b_financial", "Debit Note B2B (Financial)"),
    ("needs_review", "Needs category review"),
])


def _amount(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def _annexure_line(row: dict) -> tuple[str, int]:
    doc = normalize_document_type(row.get("document_type"))
    supply = normalize_supply_type(row.get("supply_type"))
    category = row.get("category") or row.get("classification") or "UNCLASSIFIED"
    is_credit = doc.startswith("CREDIT_NOTE")
    is_debit = doc.startswith("DEBIT_NOTE")
    is_financial = "FINANCIAL" in doc
    if is_credit or is_debit:
        prefix = "credit" if is_credit else "debit"
        if supply == "EXPORT_SERVICE":
            return f"{prefix}_export_service", -1 if is_credit else 1
        if supply in {"EXPORT", "DEEMED_EXPORT"} or category in {"EXPORT", "DEEMED_EXPORT"}:
            return f"{prefix}_export", -1 if is_credit else 1
        if supply == "SEZ" or category == "SEZ":
            return f"{prefix}_sez", -1 if is_credit else 1
        valid_gstin = False
        if row.get("customer_gstin"):
            valid_gstin = validate_gstin(str(row["customer_gstin"]))[0]
        b2b = category == "B2B" or valid_gstin
        if is_credit and is_financial:
            return ("credit_b2b_financial" if b2b else "credit_b2c_financial", -1)
        if is_debit and is_financial and b2b:
            return "debit_b2b_financial", 1
        return (f"{prefix}_b2b" if b2b else f"{prefix}_b2c", -1 if is_credit else 1)

    if supply == "BRANCH_TRANSFER":
        return ("b2b_branch_transfer" if category == "B2B" else "needs_review", 1)
    if supply == "EMPLOYEE_REIMBURSEMENT":
        return ("employee_reimbursement" if category in {"B2CL", "B2CS", "B2B"} else "needs_review", 1)
    if supply == "EXEMPT":
        return "exempt_sales", 1
    if supply == "EXPORT_SERVICE":
        return "export_sales", 1
    if supply == "DEEMED_EXPORT" or category == "DEEMED_EXPORT":
        return "deemed_export_sales", 1
    if supply == "SEZ" or category == "SEZ":
        return "b2b_sez_sales", 1
    if supply == "EXPORT" or category == "EXPORT":
        return "export_sales", 1
    if category == "B2B":
        return "b2b_sales", 1
    if category == "B2CS":
        return "b2c_small", 1
    if category == "B2CL":
        return "b2c_large", 1
    return "needs_review", 1


def build_annexure_report(invoices: list[dict], period_label: str) -> dict:
    """Aggregate prepared GSTR-1 rows into the accountant's turnover annexure."""
    buckets = {key: {"particulars": label, "records": 0, "taxable_amount": Decimal("0"),
        "cgst": Decimal("0"), "sgst": Decimal("0"), "igst": Decimal("0"), "cess": Decimal("0"),
        "export_sez": Decimal("0"), "igst_export": Decimal("0")} for key, label in ANNEXURE_LINES.items()}
    for invoice in invoices:
        key, sign = _annexure_line(invoice)
        line = buckets[key]
        taxable = _amount(invoice.get("taxable_value", invoice.get("taxableValue")))
        cgst = _amount(invoice.get("cgst", invoice.get("cgstAmount")))
        sgst = _amount(invoice.get("sgst", invoice.get("sgstAmount")))
        igst = _amount(invoice.get("igst", invoice.get("igstAmount")))
        cess = _amount(invoice.get("cess", invoice.get("cessAmount")))
        line["records"] += 1
        line["taxable_amount"] += taxable.copy_abs() * sign
        line["cgst"] += cgst.copy_abs() * sign
        line["sgst"] += sgst.copy_abs() * sign
        line["igst"] += igst.copy_abs() * sign
        line["cess"] += cess.copy_abs() * sign
        if key in {"b2b_sez_sales", "export_sales", "deemed_export_sales", "credit_export", "credit_sez", "credit_export_service",
                   "debit_export", "debit_sez", "debit_export_service"}:
            line["export_sez"] += taxable.copy_abs() * sign
        if key in {"export_sales", "credit_export", "credit_export_service", "debit_export", "debit_export_service"}:
            line["igst_export"] += igst.copy_abs() * sign
    totals = {name: sum((line[name] for line in buckets.values()), Decimal("0"))
              for name in ("taxable_amount", "cgst", "sgst", "igst", "cess", "export_sez", "igst_export")}
    totals["records"] = sum(line["records"] for line in buckets.values())
    return {"period_label": period_label, "rows": list(buckets.values()), "totals": totals,
        "note": "Export/SEZ and export-IGST columns are supplementary breakouts already included in taxable amount and tax totals."}
