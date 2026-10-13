from io import BytesIO
from datetime import date
from decimal import Decimal

from openpyxl import Workbook

from app.ingestion.gstr1_sales import b2cl_threshold, classify_invoice, normalize_document_type, parse_sales_register, reporting_category


RULES = [
    {"effective_from": "2017-07-01", "effective_to": "2024-07-31", "version": "PRE_AUG_2024", "definition": {"threshold_inr": "250000"}},
    {"effective_from": "2024-08-01", "effective_to": None, "version": "AUG_2024_PLUS", "definition": {"threshold_inr": "100000"}},
]


def test_b2cl_threshold_is_period_effective_dated():
    assert b2cl_threshold("2024-07", RULES) == (Decimal("250000"), "PRE_AUG_2024")
    assert b2cl_threshold("2024-08", RULES) == (Decimal("100000"), "AUG_2024_PLUS")


def test_classification_explains_b2b_and_b2cl():
    classification, reason, _ = classify_invoice(seller_gstin="27AAPFU0939F1ZV", customer_gstin="29AAGCB7383J1Z4",
        place_of_supply=None, invoice_value=Decimal("100001"), period="2024-08", b2cl_rules=RULES)
    assert classification == "B2B"
    assert "GSTIN" in reason
    classification, reason, version = classify_invoice(seller_gstin="27AAPFU0939F1ZV", customer_gstin=None,
        place_of_supply="29", invoice_value=Decimal("100001"), period="2024-08", b2cl_rules=RULES)
    assert classification == "B2CL"
    assert version == "AUG_2024_PLUS"
    assert "100,000" in reason


def test_document_type_and_supply_type_drive_gstr1_reporting_categories():
    assert normalize_document_type("Tax Invoice") == "INVOICE"
    assert normalize_document_type("Credit Memo") == "CREDIT_NOTE"
    assert normalize_document_type("Sales Invoice - Export with LUT") == "EXPORT"
    csv = ("Invoice No,Invoice Date,Taxable Amount,Customer GSTIN,Document Type,Supply Type\n"
           "INV-B2B,2026-09-10,100,29AAGCB7383J1Z4,Tax Invoice,Goods\n"
           "INV-EXP,2026-09-10,200,29AAGCB7383J1Z4,Tax Invoice,Export with LUT\n"
           "INV-SEZ,2026-09-10,300,29AAGCB7383J1Z4,Tax Invoice,SEZ supply\n"
           "CN-1,2026-09-10,50,29AAGCB7383J1Z4,Credit Memo,Goods\n"
           "DN-1,2026-09-10,60,29AAGCB7383J1Z4,Debit Note,Goods\n"
           "OTHER-1,2026-09-10,70,29AAGCB7383J1Z4,Journal Adjustment,Goods\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    rows = {row["invoice_number"]: row for row in parsed["rows"]}
    assert len(rows) == 6
    assert (rows["INV-B2B"]["document_type"], rows["INV-B2B"]["classification"]) == ("INVOICE", "B2B")
    assert (rows["INV-EXP"]["document_type"], rows["INV-EXP"]["classification"], rows["INV-EXP"]["supply_type"]) == ("INVOICE", "EXPORT", "EXPORT")
    assert (rows["INV-SEZ"]["document_type"], rows["INV-SEZ"]["classification"]) == ("INVOICE", "SEZ")
    assert rows["CN-1"]["classification"] == "CREDIT_NOTE"
    assert rows["DN-1"]["classification"] == "DEBIT_NOTE"
    assert (rows["OTHER-1"]["document_type"], rows["OTHER-1"]["classification"]) == ("OTHER", "B2B")
    assert any(issue["code"] == "DOCUMENT_TYPE_REQUIRES_REVIEW" for issue in rows["OTHER-1"]["issues"])
    assert reporting_category("OTHER", "GOODS", "OTHER", seller_gstin="27AAPFU0939F1ZV",
        customer_gstin="29AAGCB7383J1Z4", place_of_supply="29", invoice_value=Decimal("100"),
        period="2026-09", b2cl_rules=RULES) == "B2B"
    assert reporting_category("OTHER", "GOODS", "OTHER", seller_gstin="27AAPFU0939F1ZV",
        customer_gstin=None, place_of_supply="27", invoice_value=Decimal("100"),
        period="2026-09", b2cl_rules=RULES) == "B2CS"


def test_negative_credit_note_amounts_are_normalized_for_net_annexure_signing():
    csv = ("Invoice No,Invoice Date,Taxable Amount,CGST,SGST,Invoice Value,Document Type,Customer GSTIN\n"
           "CN-1,2026-09-10,-100,-9,-9,-118,Credit Note,29AAGCB7383J1Z4\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    row = parsed["rows"][0]
    assert row["document_type"] == "CREDIT_NOTE"
    assert row["taxable_value"] == Decimal("100.00")
    assert row["cgst"] == Decimal("9.00")
    assert row["sgst"] == Decimal("9.00")


def test_summary_pivot_file_is_rejected_with_business_friendly_message():
    from app.ingestion.column_resolution import AggregatedSalesDataError
    csv = ("Sum of Taxable Value,Sum of IGST_Rate,Sum of IGST_Amount,Count of Invoice Number\n"
           "100000,36,18000,12\n").encode()
    try:
        parse_sales_register(csv, "summary.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    except AggregatedSalesDataError as exc:
        assert "summarized totals" in str(exc)
        assert "invoice-level" in str(exc)
    else:
        raise AssertionError("Aggregated pivot data must not be processed as invoice-level sales.")


def test_optional_quantity_and_export_details_are_normalized_for_annexure():
    csv = ("Invoice No,Invoice Date,Taxable Amount,Quantity,UQC,Export Type,Shipping Bill No,Shipping Bill Date,Port Code\n"
        "EXP-1,2026-09-10,1000,2.375,KGS,Without payment under LUT,SB-25,11/09/2026,INMAA1\n").encode()
    parsed = parse_sales_register(csv, "export.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    row = parsed["rows"][0]
    assert row["quantity"] == Decimal("2.375")
    assert row["uqc"] == "KGS"
    assert row["export_type"] == "Without payment under LUT"
    assert row["shipping_bill_number"] == "SB-25"
    assert row["shipping_bill_date"] == date(2026, 9, 11)
    assert row["port_code"] == "INMAA1"


def test_classification_explains_missing_gstin_and_never_calls_invalid_gstin_b2c():
    classification, reason, _ = classify_invoice(seller_gstin="27AAPFU0939F1ZV", customer_gstin=None,
        place_of_supply="Maharashtra", invoice_value=Decimal("12000"), period="2024-08", b2cl_rules=RULES)
    assert classification == "B2CS"
    assert reason.startswith("B2C — recipient GSTIN unavailable")
    classification, reason, _ = classify_invoice(seller_gstin="27AAPFU0939F1ZV", customer_gstin="27ABCDE1234F1Z6",
        place_of_supply="27", invoice_value=Decimal("12000"), period="2024-08", b2cl_rules=RULES)
    assert classification == "UNCLASSIFIED"
    assert "failed validation" in reason


def test_import_normalizes_named_place_of_supply_and_flags_gstin_state_conflict():
    csv = ("invoice_number,invoice_date,taxable_value,invoice_value,customer_gstin,place_of_supply\n"
           "INV-POS,2024-08-10,1000,1180,29AAGCB7383J1Z4,Maharashtra\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2024-08", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["place_of_supply"] == "27"
    assert parsed["rows"][0]["classification"] == "B2B"
    assert any(issue["code"] == "RECIPIENT_GSTIN_POS_MISMATCH" for issue in parsed["issues"])


def test_sales_register_import_preserves_duplicates_as_review_issues():
    csv = ("customer_name,customer_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,invoice_value,place_of_supply,hsn_sac\n"
           "Buyer,,INV-102,01-09-2026,1000,90,90,0,1180,27,9983\n"
           "Buyer,,INV102,01-09-2026,1000,90,90,0,1180,27,9983\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert len(parsed["rows"]) == 2
    assert any(issue["code"] == "POSSIBLE_FUZZY_DUPLICATE" for issue in parsed["issues"])
    assert all(any(issue["code"] == "POSSIBLE_FUZZY_DUPLICATE" for issue in row["issues"]) for row in parsed["rows"])
    assert any("92%" in issue["message"] for issue in parsed["issues"])
    assert parsed["rows"][0]["invoice_value"] == Decimal("1180.00")
    assert parsed["rows"][0]["classification"] == "B2CS"


def test_invalid_amount_row_is_not_imported():
    csv = ("invoice_number,invoice_date,taxable_value,invoice_value\n"
           "INV-X,2026-09-10,not-a-number,100\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"] == []
    assert parsed["issues"][0]["code"] == "INVALID_AMOUNT"


def test_tax_arithmetic_and_place_of_supply_are_review_flags_not_auto_corrections():
    csv = ("invoice_number,invoice_date,taxable_value,cgst,sgst,igst,gst_rate,invoice_value,place_of_supply,hsn_sac\n"
           "INV-Z,2026-09-10,100000,7000,7000,0,18,114000,27,9983\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert len(parsed["rows"]) == 1
    codes = {issue["code"] for issue in parsed["issues"]}
    assert "TAX_RATE_ARITHMETIC" in codes
    tax_issue = next(issue for issue in parsed["issues"] if issue["code"] == "TAX_RATE_ARITHMETIC")
    assert tax_issue["severity"] == "REVIEW"
    assert "expected GST ₹18,000.00" in tax_issue["message"]
    assert "reported CGST+SGST+IGST is ₹14,000.00" in tax_issue["message"]
    assert "difference ₹4,000.00" in tax_issue["message"]
    assert "does not determine whether the entered rate is legally applicable" in tax_issue["message"]
    assert parsed["rows"][0]["taxable_value"] == Decimal("100000.00")
    assert parsed["rows"][0]["cgst"] == Decimal("7000.00")
    assert parsed["rows"][0]["sgst"] == Decimal("7000.00")
    assert parsed["rows"][0]["igst"] == Decimal("0.00")


def test_state_based_tax_split_flags_mismatches_and_accepts_consistent_splits():
    csv = ("invoice_number,invoice_date,taxable_value,cgst,sgst,igst,invoice_value,place_of_supply\n"
           "INTRA-WRONG,2026-09-10,100000,0,0,18000,118000,27\n"
           "INTER-WRONG,2026-09-10,100000,9000,9000,0,118000,29\n"
           "INTRA-OK,2026-09-11,100000,9000,9000,0,118000,27\n"
           "INTER-OK,2026-09-11,100000,0,0,18000,118000,29\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    rows = {row["invoice_number"]: row for row in parsed["rows"]}
    for invoice_number in ("INTRA-WRONG", "INTER-WRONG"):
        issue = next(issue for issue in rows[invoice_number]["issues"] if issue["code"] == "PLACE_OF_SUPPLY_TAX_SPLIT")
        assert issue["severity"] == "REVIEW"
        assert "needs review" in issue["message"]
        assert "no tax amounts were changed" in issue["message"]
    assert "same-state supply" in next(issue["message"] for issue in rows["INTRA-WRONG"]["issues"] if issue["code"] == "PLACE_OF_SUPPLY_TAX_SPLIT")
    assert "different-state supply" in next(issue["message"] for issue in rows["INTER-WRONG"]["issues"] if issue["code"] == "PLACE_OF_SUPPLY_TAX_SPLIT")
    assert not any(issue["code"] == "PLACE_OF_SUPPLY_TAX_SPLIT" for issue in rows["INTRA-OK"]["issues"])
    assert not any(issue["code"] == "PLACE_OF_SUPPLY_TAX_SPLIT" for issue in rows["INTER-OK"]["issues"])
    assert rows["INTRA-WRONG"]["igst"] == Decimal("18000.00")
    assert rows["INTER-WRONG"]["cgst"] == Decimal("9000.00")
    assert rows["INTER-WRONG"]["sgst"] == Decimal("9000.00")


def test_invoice_series_gap_is_a_review_suggestion_not_a_rejection():
    csv = ("invoice_number,invoice_date,taxable_value,invoice_value,invoice_series\n"
           "INV-101,2026-09-10,100,100,INV-\n"
           "INV-103,2026-09-12,100,100,INV-\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    gaps = [issue for issue in parsed["issues"] if issue["code"] == "INVOICE_SEQUENCE_GAP"]
    assert len(parsed["rows"]) == 2
    assert len(gaps) == 1
    assert gaps[0]["severity"] == "REVIEW"
    assert "cancellation" in gaps[0]["message"]
    assert not any(issue["code"] == "PERIOD_MISMATCH" for issue in parsed["issues"])


def test_fuzzy_duplicate_requires_multiple_matching_signals_and_stays_reviewable():
    csv = ("customer_name,customer_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,invoice_value\n"
           "Buyer,29AAGCB7383J1Z4,INV-1001,2026-09-10,1000,90,90,1180\n"
           "Buyer,29AAGCB7383J1Z4,INV-1002,2026-09-10,1000,90,90,1180\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    fuzzy = [issue for issue in parsed["issues"] if issue["code"] == "POSSIBLE_FUZZY_DUPLICATE"]
    assert len(fuzzy) == 2
    assert fuzzy[0]["severity"] == "REVIEW"
    assert len(parsed["rows"]) == 2
    assert all(any(issue["code"] == "POSSIBLE_FUZZY_DUPLICATE" for issue in row["issues"]) for row in parsed["rows"])


def test_exact_duplicate_uses_all_signals_and_number_reuse_is_not_called_a_duplicate():
    csv = ("customer_name,customer_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,invoice_value\n"
           "Buyer,29AAGCB7383J1Z4,INV-102,2026-09-10,50000,4500,4500,59000\n"
           "Buyer,29AAGCB7383J1Z4,INV-102,2026-09-10,50000,4500,4500,59000\n"
           "Buyer,29AAGCB7383J1Z4,INV-103,2026-09-10,51000,4590,4590,60180\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    codes_by_row = {row["row_number"]: {issue["code"] for issue in row["issues"]} for row in parsed["rows"]}
    assert "POSSIBLE_DUPLICATE" in codes_by_row[2]
    assert "POSSIBLE_DUPLICATE" in codes_by_row[3]
    assert "INVOICE_NUMBER_REUSED" not in codes_by_row[4]
    assert "POSSIBLE_DUPLICATE" not in codes_by_row[4]


def test_reused_invoice_number_with_different_values_is_reviewed_but_not_merged():
    csv = ("customer_name,customer_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,invoice_value\n"
           "Buyer,29AAGCB7383J1Z4,INV-102,2026-09-10,50000,4500,4500,59000\n"
           "Buyer,29AAGCB7383J1Z4,INV102,2026-09-11,60000,5400,5400,70800\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert len(parsed["rows"]) == 2
    assert any(issue["code"] == "INVOICE_NUMBER_REUSED" for issue in parsed["issues"])
    assert not any(issue["code"] == "POSSIBLE_DUPLICATE" for issue in parsed["issues"])


def test_cancelled_documents_note_links_and_hsn_format_are_flagged():
    csv = ("customer_name,customer_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,invoice_value,place_of_supply,hsn_sac,document_type,original_invoice_number,invoice_status\n"
           "Buyer,29AAGCB7383J1Z4,INV-1,2026-09-01,1000,90,90,1180,29,9983,INVOICE,,CANCELLED\n"
           "Buyer,29AAGCB7383J1Z4,CN-1,2026-09-02,1200,108,108,1416,29,12,CREDIT_NOTE,INV-1,ACTIVE\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    codes = {issue["code"] for issue in parsed["issues"]}
    assert "CANCELLED_INVOICE_INCLUDED" in codes
    assert "HSN_FORMAT_REVIEW" in codes
    assert "NOTE_EXCEEDS_ORIGINAL" in codes
    assert parsed["rows"][1]["original_invoice_number"] == "INV-1"


def test_hsn_rate_reference_mismatch_is_a_review_suggestion():
    csv = ("invoice_number,invoice_date,taxable_value,cgst,sgst,igst,gst_rate,invoice_value,place_of_supply,hsn_sac\n"
           "INV-RATE,2026-09-10,10000,900,900,0,18,11800,27,9983\n").encode()
    reference = [{"version": "TEST_RATE_V1", "effective_from": "2025-01-01", "effective_to": None,
        "definition": {"rates_by_hsn": {"9983": "12"}}}]
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES, reference)
    rate_issue = next(issue for issue in parsed["issues"] if issue["code"] == "GST_RATE_REFERENCE_MISMATCH")
    assert rate_issue["severity"] == "REVIEW"
    assert "configured reference rate option(s) 12%" in rate_issue["message"]
    assert "reference-rate GST amount(s) would be ₹1,200.00" in rate_issue["message"]
    assert "not a definitive rate determination" in rate_issue["message"]


def test_gst_rate_reference_is_effective_dated_and_unmapped_hsn_is_flagged():
    csv = ("invoice_number,invoice_date,taxable_value,cgst,sgst,igst,gst_rate,invoice_value,hsn_sac\n"
           "INV-OLD,2024-12-31,10000,900,900,0,18,11800,9983\n"
           "INV-NEW,2025-01-01,10000,900,900,0,18,11800,9983\n"
           "INV-UNMAPPED,2025-01-02,10000,900,900,0,18,11800,9971\n").encode()
    references = [
        {"version": "OLD_RATE", "effective_from": "2024-01-01", "effective_to": "2024-12-31",
         "definition": {"rates_by_hsn": {"9983": "18"}}},
        {"version": "NEW_RATE", "effective_from": "2025-01-01", "effective_to": None,
         "definition": {"rates_by_hsn": {"9983": "12"}}},
    ]
    parsed = parse_sales_register(csv, "sales.csv", "2025-01", "27AAPFU0939F1ZV", RULES, references)
    issues_by_row = {row["row_number"]: {issue["code"] for issue in row["issues"]} for row in parsed["rows"]}
    assert "GST_RATE_REFERENCE_MISMATCH" not in issues_by_row[2]
    assert "GST_RATE_REFERENCE_MISMATCH" in issues_by_row[3]
    assert "GST_RATE_REFERENCE_NOT_FOUND" in issues_by_row[4]


def test_missing_invoice_rate_is_flagged_when_hsn_reference_exists():
    csv = ("invoice_number,invoice_date,taxable_value,cgst,sgst,invoice_value,hsn_sac\n"
           "INV-NO-RATE,2026-09-10,10000,900,900,11800,9983\n").encode()
    reference = [{"version": "TEST_RATE_V1", "effective_from": "2025-01-01", "effective_to": None,
        "definition": {"rates_by_hsn": {"9983": "18"}}}]
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES, reference)
    assert any(issue["code"] == "GST_RATE_MISSING" for issue in parsed["issues"])


def test_column_resolution_handles_client_accounting_labels_and_header_variants():
    headers = [
        "Party Name,GSTIN,Bill No,Bill Date,Taxable Amount,Central Tax,State Tax,Integrated Tax,Total Amount,POS,HSN Code,Tax %",
        "Customer,Customer GST No,Invoice No.,Invoice/Bill Date,Taxable Amt.,CGST Amount,SGST Amount,IGST Amount,Invoice Value,Place of Supply,HSN/SAC,GST Rate",
        "party-name,party_gst_no,invoice_no,document-date,taxable-value,cgst_amt,sgst_amt,igst_amt,grand-total,supply-state,sac-code,tax-percent",
        "Buyer,Recipient GSTIN,Voucher No,Voucher Date,Assessable Value,CGST,SGST,IGST,Bill Value,Destination State,HSN,Tax Rate",
    ]
    for index, header in enumerate(headers):
        values = ["Acme", "29AAGCB7383J1Z4", f"INV-{index + 1}", "15/09/2026", '"₹1,00,000.00"',
                  '"9,000"', '"9,000"', "0", '"₹1,18,000"', "Karnataka", "998314.0", "18%"]
        parsed = parse_sales_register((header + "\n" + ",".join(values) + "\n").encode(),
            "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
        assert len(parsed["rows"]) == 1, (header, parsed["issues"])
        row = parsed["rows"][0]
        assert row["invoice_number"] == f"INV-{index + 1}"
        assert row["customer_name"] == "Acme"
        assert row["customer_gstin"] == "29AAGCB7383J1Z4"
        assert row["invoice_date"] == date(2026, 9, 15)
        assert row["taxable_value"] == Decimal("100000.00")
        assert row["invoice_value"] == Decimal("118000.00")
        assert row["gst_rate"] == Decimal("18.00")
        assert row["place_of_supply"] == "29"
        assert row["hsn_sac"] == "998314"


def test_header_detection_supports_title_rows_reordered_columns_and_new_optional_fields():
    csv = ("September Sales Register,,,,\n"
           "Prepared for internal review,,,,\n"
           "Taxable Amt,Invoice/Bill Date,New Client Reference,Party Name,Bill No\n"
           '"₹1,000.00",15/09/2026,ABC,Acme,INV-1\n').encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert len(parsed["rows"]) == 1
    assert parsed["rows"][0]["row_number"] == 4
    assert parsed["rows"][0]["customer_name"] == "Acme"
    assert parsed["rows"][0]["invoice_date"] == date(2026, 9, 15)


def test_ambiguous_generic_date_header_fails_safely_with_accountant_language():
    csv = ("Invoice No,Date,Taxable Amount\nINV-1,15/09/2026,1000\n").encode()
    try:
        parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
        assert False, "ambiguous Date should not be guessed"
    except ValueError as exc:
        assert "needs clarification" in str(exc)
        assert "invoice_date" not in str(exc)


def test_distinctive_gstin_values_can_resolve_an_unfamiliar_customer_header():
    csv = ("Invoice No,Invoice Date,Taxable Amount,Account Tax Identity\n"
           "INV-1,15/09/2026,1000,29AAGCB7383J1Z4\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["customer_gstin"] == "29AAGCB7383J1Z4"


def test_identical_recipient_gstin_columns_are_collapsed_automatically():
    csv = ("Invoice No,Invoice Date,Taxable Amount,N: Customer_Bill_To_GSTIN,Customer_Shipping/Billing_GSTIN\n"
           "INV-1,15/09/2026,1000,29AAGCB7383J1Z4,29AAGCB7383J1Z4\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["customer_gstin"] == "29AAGCB7383J1Z4"
    mapped_gstin_columns = [name for name, field in parsed["_format_profile"]["column_rules"].items()
        if field == "customer_gstin"]
    assert mapped_gstin_columns == ["N: Customer_Bill_To_GSTIN"]


def test_conflicting_bill_to_and_ship_to_gstins_uses_explicit_bill_to_column():
    csv = ("Invoice No,Invoice Date,Taxable Amount,Customer_Bill_To_GSTIN,Customer_Shipping_GSTIN\n"
           "INV-1,15/09/2026,1000,29AAGCB7383J1Z4,27ABCDE1234F1Z5\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["customer_gstin"] == "29AAGCB7383J1Z4"


def test_conflicting_same_meaning_columns_still_require_clarification():
    csv = ("Invoice No,Invoice Date,Taxable Amount,CGST,Central Tax\n"
           "INV-1,15/09/2026,1000,90,80\n").encode()
    try:
        parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
        assert False, "conflicting tax columns must not be silently discarded"
    except ValueError as exc:
        assert "conflicting" in str(exc)


def test_csv_utf16_semicolon_export_is_read_as_a_sales_register():
    source = ("Invoice No;Invoice Date;Taxable Amount;Customer GSTIN\n"
              "INV-UTF16;15/09/2026;1000;29AAGCB7383J1Z4\n").encode("utf-16")
    parsed = parse_sales_register(source, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["invoice_number"] == "INV-UTF16"
    assert parsed["rows"][0]["customer_gstin"] == "29AAGCB7383J1Z4"


def test_excel_workbook_misnamed_as_csv_gets_actionable_message():
    try:
        parse_sales_register(b"PK\x03\x04not-a-csv", "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
        assert False, "an Excel workbook must not be parsed as CSV"
    except ValueError as exc:
        assert "Excel workbook saved with a .csv name" in str(exc)


def test_repeated_csv_headings_are_compared_by_column_and_identical_copy_is_dropped():
    csv = ("Invoice No,Invoice Date,Taxable Amount,CGST,CGST\n"
           "INV-1,15/09/2026,1000,90,90\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["cgst"] == Decimal("90.00")
    mapped_cgst_columns = [name for name, field in parsed["_format_profile"]["column_rules"].items()
        if field == "cgst"]
    assert mapped_cgst_columns == ["CGST"]


def test_repeated_csv_headings_with_conflicting_values_return_clear_conflict():
    csv = ("Invoice No,Invoice Date,Taxable Amount,CGST,CGST\n"
           "INV-1,15/09/2026,1000,90,80\n").encode()
    try:
        parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
        assert False, "conflicting repeated tax headers must not be silently accepted"
    except ValueError as exc:
        assert "conflicting" in str(exc)


def test_conflicting_direct_and_aggregated_taxable_columns_require_confirmation():
    csv = ("Invoice No,Invoice Date,Taxable Value,Sum of Taxable Value\n"
           "INV-1,15/09/2026,1000,1001\n").encode()
    try:
        parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
        assert False, "different detail and aggregate values must not be selected by header priority"
    except ValueError as exc:
        assert "conflicting taxable amount" in str(exc)


def test_place_of_supply_state_names_and_codes_are_equivalent_and_recorded():
    csv = ("Invoice No,Invoice Date,Taxable Amount,Place of Supply,POS State\n"
           "INV-1,15/09/2026,1000,Maharashtra (27),27\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["place_of_supply"] == "27"
    decision = next(item for item in parsed["_format_profile"]["resolution_decisions"] if item["business_field"] == "place of supply")
    assert decision["decision"] == "AUTO_EQUIVALENT_COLUMNS"


def test_place_of_supply_merges_missing_value_from_equivalent_column():
    csv = ("Invoice No,Invoice Date,Taxable Amount,Place of Supply,POS State\n"
           "INV-1,15/09/2026,1000,,27\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["place_of_supply"] == "27"
    decision = next(item for item in parsed["_format_profile"]["resolution_decisions"] if item["business_field"] == "place of supply")
    assert decision["decision"] == "MERGED_MISSING_VALUES"


def test_conflicting_valid_place_of_supply_values_return_affected_rows_and_choices():
    from app.ingestion.column_resolution import AmbiguousBusinessColumnError
    csv = ("Invoice No,Invoice Date,Taxable Amount,Place of Supply,POS State\n"
           "INV-1,15/09/2026,1000,Maharashtra,Karnataka\n"
           "INV-2,16/09/2026,2000,27,29\n").encode()
    try:
        parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
        assert False, "different valid states must not be guessed"
    except AmbiguousBusinessColumnError as exc:
        assert exc.field == "place_of_supply"
        assert exc.affected_row_count == 2
        assert {column["name"] for column in exc.columns} == {"Place of Supply", "POS State"}
        assert exc.sample_rows[0]["values"]["Place of Supply"] == "Maharashtra"


def test_confirmed_place_of_supply_choice_is_applied_and_auditable():
    csv = ("Invoice No,Invoice Date,Taxable Amount,Place of Supply,POS State\n"
           "INV-1,15/09/2026,1000,Maharashtra,Karnataka\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES,
        business_column_choices={"POS State": "place_of_supply"})
    assert parsed["rows"][0]["place_of_supply"] == "29"
    decision = next(item for item in parsed["_format_profile"]["resolution_decisions"] if item["business_field"] == "place of supply")
    assert decision["decision"] == "ACCOUNTANT_CONFIRMED_SOURCE"
    assert decision["affected_source_rows"] == [2]


def test_explicit_choice_distinguishes_identically_named_duplicate_headers():
    csv = ("Invoice No,Invoice Date,Taxable Amount,CGST,CGST\n"
           "INV-1,15/09/2026,1000,90,80\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES,
        business_column_choices={"CGST [duplicate 2]": "cgst"})
    assert parsed["rows"][0]["cgst"] == Decimal("80.00")
    decision = next(item for item in parsed["_format_profile"]["resolution_decisions"] if item["business_field"] == "CGST amount")
    assert decision["selected_column"] == "CGST [duplicate 2]"


def test_aggregated_igst_rate_is_not_read_as_overall_gst_rate():
    csv = ("Invoice No,Invoice Date,Taxable Amount,Sum of IGST_Rate,IGST Amount\n"
           "INV-1,15/09/2026,1000,36,180\n").encode()
    parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["gst_rate"] is None


def test_customer_billing_and_shipping_states_are_not_place_of_supply_aliases():
    for heading in ("Billing State", "Shipping State", "Customer State"):
        csv = (f"Invoice No,Invoice Date,Taxable Amount,{heading}\n"
               "INV-1,15/09/2026,1000,Karnataka\n").encode()
        parsed = parse_sales_register(csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
        assert parsed["rows"][0]["place_of_supply"] is None, f"{heading} alone must not be inferred as place of supply"


def test_saved_layout_does_not_suppress_new_place_of_supply_conflicts():
    first_csv = ("Invoice No,Invoice Date,Taxable Amount,Place of Supply,POS State\n"
                 "INV-1,15/09/2026,1000,27,27\n").encode()
    first = parse_sales_register(first_csv, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    profile = first["_format_profile"]
    conflict = ("Invoice No,Invoice Date,Taxable Amount,Place of Supply,POS State\n"
                "INV-2,16/09/2026,1000,27,29\n").encode()
    try:
        parse_sales_register(conflict, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES,
            known_profiles=[{"fingerprint": profile["fingerprint"], "column_rules": profile["column_rules"],
                "confirmed_columns": profile["confirmed_columns"]}])
        assert False, "a saved layout must not override current conflicts"
    except ValueError as exc:
        assert "conflicting place of supply" in str(exc)


def test_separate_invoice_prefix_is_combined_without_overwriting_source_file():
    source = ("Invoice Prefix,Invoice No,Invoice Date,Taxable Amount\nINV,1023,15/09/2026,1000\n").encode()
    parsed = parse_sales_register(source, "sales.csv", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert parsed["rows"][0]["invoice_number"] == "INV-1023"
    assert parsed["source_sha256"]
    assert parsed["_format_profile"]["column_rules"]["Invoice Prefix"] == "invoice_series"


def test_excel_register_with_export_title_row_and_excel_serial_date_is_normalized():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Monthly outward supplies", None, None])
    sheet.append(["Bill No", "Bill Date", "Taxable Amt"])
    sheet.append(["INV-XLSX-1", 46271, "₹1,250.50"])
    sheet["B3"].number_format = "0"
    contents = BytesIO()
    workbook.save(contents)
    parsed = parse_sales_register(contents.getvalue(), "sales.xlsx", "2026-09", "27AAPFU0939F1ZV", RULES)
    assert len(parsed["rows"]) == 1
    assert parsed["rows"][0]["invoice_number"] == "INV-XLSX-1"
    assert parsed["rows"][0]["taxable_value"] == Decimal("1250.50")
    assert parsed["rows"][0]["invoice_date"] == date(2026, 9, 6)
