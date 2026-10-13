from decimal import Decimal

from app.ingestion.gstr1_annexure import build_annexure_report


def test_annexure_report_groups_sales_notes_and_export_breakouts():
    invoices = [
        {"document_type": "INVOICE", "supply_type": "GOODS", "category": "B2B", "taxable_value": 100,
         "cgst": 9, "sgst": 9, "igst": 0, "cess": 0},
        {"document_type": "INVOICE", "supply_type": "GOODS", "category": "B2CS", "taxable_value": 200,
         "cgst": 0, "sgst": 0, "igst": 36, "cess": 0},
        {"document_type": "INVOICE", "supply_type": "EXPORT", "category": "EXPORT", "taxable_value": 300,
         "cgst": 0, "sgst": 0, "igst": 54, "cess": 0},
        {"document_type": "INVOICE", "supply_type": "SEZ", "category": "SEZ", "taxable_value": 400,
         "cgst": 0, "sgst": 0, "igst": 72, "cess": 0},
        {"document_type": "CREDIT_NOTE_FINANCIAL", "supply_type": "GOODS", "category": "CREDIT_NOTE",
         "customer_gstin": "29AAGCB7383J1Z4", "taxable_value": 50, "cgst": 4.5, "sgst": 4.5, "igst": 0, "cess": 0},
        {"document_type": "DEBIT_NOTE", "supply_type": "GOODS", "category": "DEBIT_NOTE",
         "customer_gstin": None, "taxable_value": 25, "cgst": 0, "sgst": 0, "igst": 4.5, "cess": 0},
    ]
    report = build_annexure_report(invoices, "Aug-26 turnover")
    rows = {row["particulars"]: row for row in report["rows"]}
    assert report["period_label"] == "Aug-26 turnover"
    assert rows["B2B Sales"]["taxable_amount"] == Decimal("100")
    assert rows["B2C Sales - Small"]["taxable_amount"] == Decimal("200")
    assert rows["Export Sales"]["export_sez"] == Decimal("300")
    assert rows["Export Sales"]["igst_export"] == Decimal("54")
    assert rows["B2B (SEZ) Sales"]["export_sez"] == Decimal("400")
    assert rows["Credit Note B2B (Financial)"]["taxable_amount"] == Decimal("-50")
    assert rows["Credit Note B2B (Financial)"]["cgst"] == Decimal("-4.5")
    assert rows["Debit Notes - B2C"]["taxable_amount"] == Decimal("25")
    assert report["totals"]["records"] == 6
    assert report["totals"]["taxable_amount"] == Decimal("975")
