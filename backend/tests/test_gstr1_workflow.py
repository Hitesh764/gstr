import json
from datetime import date, datetime

from app import models
from app.security import hash_password


def test_control_center_hsn_groups_other_source_documents_by_gst_reporting_category(client, login, db, two_firms):
    world = two_firms["a"]
    db.add(models.GSTR1RuleDefinition(id="test-b2cl-rule-document-report", rule_key="B2CL_THRESHOLD", version="TEST_2024_08",
        definition={"threshold_inr": "100000"}, source_url="https://tutorial.gst.gov.in/",
        effective_from=date(2024, 8, 1), effective_to=None, verified_at=datetime.utcnow()))
    db.commit()
    headers = login(world["users"]["associate"].email)
    csv = ("Invoice No,Invoice Date,Taxable Amount,Customer GSTIN,Place of Supply,HSN/SAC,Document Type\n"
           "DOC-1,2026-09-10,100,29AAGCB7383J1Z4,29,9983,Journal Adjustment\n"
           "DOC-2,2026-09-11,200,,27,9983,Journal Adjustment\n").encode()
    uploaded = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        files={"file": ("sales.csv", csv, "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    workspace = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09", headers=headers).json()
    assert workspace["summary"]["categories"]["B2B"]["count"] == 1
    assert workspace["summary"]["categories"]["B2CS"]["count"] == 1
    assert workspace["hsnSummary"]["9983|B2B"]["invoiceCount"] == 1
    assert workspace["hsnSummary"]["9983|B2C"]["invoiceCount"] == 1
    assert "9983|OTHER" not in workspace["hsnSummary"]
    assert workspace["summary"]["documentTypes"]["OTHER"]["count"] == 2


def test_normalized_annexure_reports_are_filtered_paginated_and_exportable(client, login, db, two_firms):
    world = two_firms["a"]
    db.add(models.GSTR1RuleDefinition(id="annexure-b2cl-rule", rule_key="B2CL_THRESHOLD", version="ANNEXURE_TEST",
        definition={"threshold_inr":"100000"}, source_url="https://tutorial.gst.gov.in/",
        effective_from=date(2024, 8, 1), effective_to=None, verified_at=datetime.utcnow()))
    db.commit()
    headers = login(world["users"]["associate"].email)
    source = ("Invoice No,Invoice Date,Taxable Amount,Customer Name,Customer GSTIN,Place of Supply,HSN/SAC,Quantity,UQC,GST Rate,CGST,SGST,Invoice Value\n"
        "ANN-1,2026-09-10,1000,Buyer,29AAGCB7383J1Z4,29,9983,2,NOS,18,90,90,1180\n"
        "ANN-2,2026-09-11,500,Walk In,,27,9983,1,NOS,5,12.5,12.5,525\n").encode()
    upload = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        files={"file":("normalized.csv",source,"text/csv")})
    assert upload.status_code == 200, upload.text
    response = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/annexure?report_type=b2b&page=1&page_size=1", headers=headers)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["pagination"] == {"page":1,"pageSize":1,"total":1,"pages":1}
    assert page["rows"][0]["invoiceNumber"] == "ANN-1"
    assert page["rows"][0]["gstr1Table"].startswith("Table 4A")
    assert page["rows"][0]["tableMappingVersion"] == "GSTR1_TABLE_MAP_2025_05"
    assert page["rows"][0]["validationStatus"] in {"READY","REVIEW_REQUIRED"}
    hsn = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/annexure?report_type=hsn", headers=headers)
    assert hsn.status_code == 200, hsn.text
    hsn_b2b = next(row for row in hsn.json()["rows"] if row["gstRate"] == 18.0)
    assert hsn_b2b["quantity"] == 2.0
    assert hsn_b2b["uqc"] == "NOS"
    workbook = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/annexure-workbook.xlsx", headers=headers)
    assert workbook.status_code == 200, workbook.text
    assert b"PK" == workbook.content[:2]
    csv_export = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/annexure.csv", headers=headers)
    assert csv_export.status_code == 200, csv_export.text
    assert "GSTR-1 Table" in csv_export.text and "ANN-1" in csv_export.text
    pdf_export = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/annexure.pdf", headers=headers)
    assert pdf_export.status_code == 200, pdf_export.text
    assert pdf_export.content.startswith(b"%PDF-1.4")
    for report_type in ("b2b", "b2c", "hsn_b2b", "hsn_summary", "exports", "notes"):
        separate = client.get(
            f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/annexure-report.xlsx?report_type={report_type}",
            headers=headers)
        assert separate.status_code == 200, separate.text
        assert separate.content.startswith(b"PK")


def test_sales_upload_requires_separate_manager_and_partner_approval(client, login, db, two_firms):
    world = two_firms["a"]
    associate = world["users"]["associate"]
    manager = world["users"]["manager"]
    partner = models.User(firm_id=world["firm"].id, name="Partner Alpha", email="partner@alpha-ca.in",
        role=models.UserRole.PARTNER, password_hash=hash_password("test-password-123"), email_verified=True)
    db.add(partner)
    db.add(models.GSTR1RuleDefinition(id="test-b2cl-rule", rule_key="B2CL_THRESHOLD", version="TEST_2024_08",
        definition={"threshold_inr": "100000"},
        source_url="https://tutorial.gst.gov.in/userguide/returns/Creation_of_Outward_Supplies_Return_in_GSTR-1.htm",
        effective_from=date(2024, 8, 1), effective_to=None, verified_at=datetime.utcnow()))
    db.commit()

    headers = login(associate.email)
    csv = ("customer_name,customer_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,invoice_value,place_of_supply,hsn_sac,gst_rate\n"
           "Buyer,29AAGCB7383J1Z4,OUT-001,2026-09-10,1000,90,90,0,1180,29,9983,18\n").encode()
    response = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09",
        headers=headers, files={"file": ("sales.csv", csv, "text/csv")})
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["status"] == "DRAFT"
    assert created["recordsProcessed"] == 1
    assert created["recordsAccepted"] == 0
    assert created["recordsRequiringReview"] == 1
    assert "mapping" not in created and "issues" not in created
    profile = db.query(models.GSTR1ClientFormatProfile).filter_by(client_gstin_id=world["gstin"].id).one()
    saved_upload = db.query(models.GSTR1SalesUpload).filter_by(id=created["uploadId"]).one()
    assert profile.import_count == 1 and profile.success_count == 1
    assert saved_upload.normalization_snapshot["alias_version"] == "gstr1-sales-aliases-v5"
    assert "invoice_date" not in created and "column_rules" not in created
    source = client.get(f"/api/gstr1/uploads/{created['uploadId']}/source", headers=headers)
    assert source.status_code == 200, source.text
    assert source.content == csv

    partner_headers = login(partner.email)
    early_approval = client.post(f"/api/gstr1/returns/{created['returnId']}/approve", headers=partner_headers)
    assert early_approval.status_code == 409

    workspace = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09", headers=headers).json()
    assert workspace["summary"]["invoiceCount"] == 1
    assert workspace["summary"]["categories"]["B2B"]["count"] == 1
    assert workspace["summary"]["documentTypes"]["INVOICE"]["count"] == 1
    assert workspace["invoices"][0]["documentType"] == "INVOICE"
    assert workspace["invoices"][0]["supplyType"] == "GOODS"
    assert workspace["annexureReport"]["periodLabel"] == "Sep-26 turnover"
    assert workspace["annexureReport"]["rows"][0]["particulars"] == "B2B Sales"
    assert workspace["annexureReport"]["rows"][0]["taxableAmount"] == 1000.0
    assert "invoice_count" not in workspace["summary"]
    invoice_id = workspace["invoices"][0]["id"]
    assert workspace["hsnSummary"]["9983|B2B"]["reportingGroup"] == "B2B"
    assert "hsn_summary" not in workspace
    assert len(client.get(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers).json()) == 1
    manager_headers = login(manager.email)
    self_review = client.post(f"/api/gstr1/invoices/{invoice_id}/review", headers=headers, json={"decision": "reviewed"})
    assert self_review.status_code == 403
    flagged_invoice = next(row for row in workspace["invoices"] if row["id"] == invoice_id)
    for issue in flagged_invoice["issues"]:
        resolved_issue = client.post(f"/api/gstr1/issues/{issue['id']}/decision", headers=manager_headers,
            json={"decision": "ACCEPT_VALID", "note": "Reviewed source invoice and supporting data."})
        assert resolved_issue.status_code == 200, resolved_issue.text
    doc_record = client.post(f"/api/gstr1/returns/{created['returnId']}/documents-issued", headers=headers,
        json={"document_type": "INVOICE", "series_from": "OUT-001", "series_to": "OUT-001",
              "issued_count": 1, "cancelled_count": 0})
    assert doc_record.status_code == 200
    self_review_docs = client.post(f"/api/gstr1/documents-issued/{doc_record.json()['id']}/review", headers=headers)
    assert self_review_docs.status_code == 403
    manager_review_docs = client.post(f"/api/gstr1/documents-issued/{doc_record.json()['id']}/review", headers=manager_headers)
    assert manager_review_docs.status_code == 200
    workbook = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/draft.xlsx", headers=headers)
    assert workbook.status_code == 200
    assert workbook.content[:2] == b"PK"
    annexure = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/annexure.xlsx", headers=headers)
    assert annexure.status_code == 200, annexure.text
    assert annexure.content[:2] == b"PK"
    assert "annexure.xlsx" in annexure.headers["content-disposition"]
    submitted = client.post(f"/api/gstr1/returns/{created['returnId']}/submit-review", headers=manager_headers)
    assert submitted.status_code == 200, submitted.text
    approved = client.post(f"/api/gstr1/returns/{created['returnId']}/approve", headers=partner_headers)
    assert approved.status_code == 200
    exported = client.get(f"/api/gstr1/returns/{created['returnId']}/approved-export", headers=partner_headers)
    assert exported.status_code == 200
    assert exported.json()["document_status"].endswith("NOT FILED")
    checklist = client.get(f"/api/gstr1/returns/{created['returnId']}/checklist", headers=partner_headers)
    assert checklist.status_code == 200
    assert checklist.json()["filed"] is False
    assert checklist.json()["ready_for_filing"] is False
    assert next(row for row in checklist.json()["checks"] if row["key"] == "external_reconciliation")["status"] == "BLOCKED"
    draft_after_approval = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09/draft.xlsx", headers=headers)
    assert draft_after_approval.status_code == 409


def test_books_reconciliation_and_issue_decisions_are_persisted(client, login, db, two_firms):
    world = two_firms["a"]
    associate = world["users"]["associate"]
    manager = world["users"]["manager"]
    db.add(models.GSTR1RuleDefinition(id="test-b2cl-rule-reconcile", rule_key="B2CL_THRESHOLD", version="TEST_2024_08",
        definition={"threshold_inr": "100000"}, source_url="https://tutorial.gst.gov.in/", effective_from=date(2024, 8, 1),
        effective_to=None, verified_at=datetime.utcnow()))
    db.commit()
    associate_headers = login(associate.email)
    source = ("customer_name,customer_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,invoice_value,place_of_supply,hsn_sac,gst_rate\n"
              "Buyer,,INV-1,2026-09-10,1000,70,70,0,1140,27,9983,18\n").encode()
    uploaded = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=associate_headers,
        files={"file": ("sales.csv", source, "text/csv")}).json()
    manager_headers = login(manager.email)
    workspace = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09", headers=associate_headers).json()
    tax_check = workspace["invoices"][0]["taxCheck"]
    assert tax_check == {"enteredRate": 18.0, "expectedGst": 180.0, "reportedGst": 140.0, "difference": 40.0}
    assert workspace["invoices"][0]["cgstAmount"] == 70.0
    assert workspace["invoices"][0]["sgstAmount"] == 70.0
    issue = next(x for x in workspace["invoices"][0]["issues"] if x["code"] == "TAX_RATE_ARITHMETIC")
    correction = client.post(f"/api/gstr1/issues/{issue['id']}/decision", headers=manager_headers,
        json={"decision": "NEEDS_CORRECTION", "note": "Please confirm the invoice tax amount."})
    assert correction.status_code == 200, correction.text
    assert correction.json()["status"] == "OPEN"
    decision = client.post(f"/api/gstr1/issues/{issue['id']}/decision", headers=manager_headers,
        json={"decision": "ACCEPT_VALID", "note": "Checked source invoice and supporting rate."})
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "RESOLVED"
    books = ("customer_name,customer_gstin,invoice_number,invoice_date,taxable_value,cgst,sgst,igst,invoice_value,place_of_supply,hsn_sac,gst_rate\n"
             "Buyer,,INV-1,2026-09-10,1100,99,99,0,1298,27,9983,18\n"
             "Buyer,,INV-2,2026-09-11,500,45,45,0,590,27,9983,18\n").encode()
    reconciled = client.post(f"/api/gstr1/returns/{uploaded['returnId']}/reconcile-books", headers=associate_headers,
        files={"file": ("books.csv", books, "text/csv")})
    assert reconciled.status_code == 200, reconciled.text
    assert len(reconciled.json()["missing_from_preparation"]) == 1
    assert len(reconciled.json()["amount_mismatches"]) == 1
    persisted = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09", headers=associate_headers).json()
    assert persisted["reconciliation"]["filename"] == "books.csv"
    assert persisted["invoices"][0]["issues"][0]["reviewNote"] == "Checked source invoice and supporting rate."


def test_workspace_returns_state_tax_split_issue_without_changing_components(client, login, db, two_firms):
    world = two_firms["a"]
    db.add(models.GSTR1RuleDefinition(id="test-b2cl-tax-split", rule_key="B2CL_THRESHOLD", version="TEST_2024_08",
        definition={"threshold_inr": "100000"}, source_url="https://tutorial.gst.gov.in/",
        effective_from=date(2024, 8, 1), effective_to=None, verified_at=datetime.utcnow()))
    db.commit()
    headers = login(world["users"]["associate"].email)
    csv = ("invoice_number,invoice_date,taxable_value,cgst,sgst,igst,invoice_value,place_of_supply\n"
           "INV-WRONG-SPLIT,2026-09-10,100000,0,0,18000,118000,27\n").encode()
    uploaded = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        files={"file": ("sales.csv", csv, "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    workspace = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09", headers=headers).json()
    invoice = workspace["invoices"][0]
    issue = next(issue for issue in invoice["issues"] if issue["code"] == "PLACE_OF_SUPPLY_TAX_SPLIT")
    assert issue["severity"] == "REVIEW"
    assert "same-state supply" in issue["message"]
    assert "IGST ₹18,000.00" in issue["message"]
    assert "invoice_date" not in invoice and "taxable_value" not in invoice
    assert invoice["invoiceDate"] == "2026-09-10"
    assert invoice["igstAmount"] == 18000.0
    assert invoice["cgstAmount"] == invoice["sgstAmount"] == 0.0


def test_ambiguous_date_can_be_clarified_and_remembered_for_the_client(client, login, db, two_firms):
    world = two_firms["a"]
    db.add(models.GSTR1RuleDefinition(id="test-b2cl-date-profile", rule_key="B2CL_THRESHOLD", version="TEST_2024_08",
        definition={"threshold_inr": "100000"}, source_url="https://tutorial.gst.gov.in/",
        effective_from=date(2024, 8, 1), effective_to=None, verified_at=datetime.utcnow()))
    db.commit()
    headers = login(world["users"]["associate"].email)
    csv = b"Invoice No,Date,Taxable Amount\nINV-DATE,15/09/2026,1000\n"
    first = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        files={"file": ("sales.csv", csv, "text/csv")})
    assert first.status_code == 422
    detail = first.json()["detail"]
    assert detail["ambiguousDate"]["column"] == "Date"
    assert "invoice_date" not in str(detail)

    clarified = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        data={"date_interpretation": "invoiceDate", "date_column": "Date"},
        files={"file": ("sales.csv", csv, "text/csv")})
    assert clarified.status_code == 200, clarified.text
    profile = db.query(models.GSTR1ClientFormatProfile).filter_by(client_gstin_id=world["gstin"].id).one()
    assert profile.column_rules["Date"] == "invoice_date"

    remembered = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        files={"file": ("sales.csv", csv, "text/csv")})
    assert remembered.status_code == 200, remembered.text
    assert remembered.json()["recordsProcessed"] == 1
    db.refresh(profile)
    assert profile.import_count == 2


def test_place_of_supply_conflict_is_explained_confirmed_and_rechecked(client, login, db, two_firms):
    world = two_firms["a"]
    db.add(models.GSTR1RuleDefinition(id="test-b2cl-pos-conflict", rule_key="B2CL_THRESHOLD", version="TEST_2024_08",
        definition={"threshold_inr": "100000"}, source_url="https://tutorial.gst.gov.in/",
        effective_from=date(2024, 8, 1), effective_to=None, verified_at=datetime.utcnow()))
    db.commit()
    headers = login(world["users"]["associate"].email)
    conflict_csv = ("Invoice No,Invoice Date,Taxable Amount,Place of Supply,POS State\n"
        "INV-POS,15/09/2026,1000,Maharashtra,Karnataka\n").encode()
    url = f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09"
    unresolved = client.post(url, headers=headers, files={"file": ("sales.csv", conflict_csv, "text/csv")})
    assert unresolved.status_code == 422
    detail = unresolved.json()["detail"]
    assert detail["businessConflict"]["field"] == "placeOfSupply"
    assert detail["businessConflict"]["affectedRowCount"] == 1
    assert {column["name"] for column in detail["businessConflict"]["columns"]} == {"Place of Supply", "POS State"}
    assert "place_of_supply" not in str(detail)
    assert db.query(models.GSTR1SalesUpload).filter_by(client_gstin_id=world["gstin"].id).count() == 0

    confirmed = client.post(url, headers=headers, data={"business_column_choices": json.dumps({"placeOfSupply": "POS State"})},
        files={"file": ("sales.csv", conflict_csv, "text/csv")})
    assert confirmed.status_code == 200, confirmed.text
    upload = db.query(models.GSTR1SalesUpload).filter_by(id=confirmed.json()["uploadId"]).one()
    snapshot = upload.normalization_snapshot
    choice = next(item for item in snapshot["resolution_decisions"] if item["business_field"] == "place of supply")
    assert choice["decision"] == "ACCOUNTANT_CONFIRMED_SOURCE"
    assert choice["selected_column"] == "POS State"
    invoice = db.query(models.GSTR1SalesInvoice).filter_by(upload_id=upload.id).one()
    assert invoice.place_of_supply == "29"

    changed_data = ("Invoice No,Invoice Date,Taxable Amount,Place of Supply,POS State\n"
        "INV-POS-2,16/09/2026,1000,27,29\n").encode()
    repeated_conflict = client.post(url, headers=headers, files={"file": ("sales.csv", changed_data, "text/csv")})
    assert repeated_conflict.status_code == 422
    assert repeated_conflict.json()["detail"]["businessConflict"]["affectedRowCount"] == 1


def test_accountant_can_clarify_unfamiliar_headings_and_client_profile_survives_layout_changes(client, login, db, two_firms):
    world = two_firms["a"]
    db.add(models.GSTR1RuleDefinition(id="test-b2cl-custom-format", rule_key="B2CL_THRESHOLD", version="TEST_2024_08",
        definition={"threshold_inr": "100000"}, source_url="https://tutorial.gst.gov.in/",
        effective_from=date(2024, 8, 1), effective_to=None, verified_at=datetime.utcnow()))
    db.commit()
    headers = login(world["users"]["associate"].email)
    initial = b"Record Ref,Net Value,When\nINV-NEW,1000,15/09/2026\n"
    first = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        files={"file": ("custom.csv", initial, "text/csv")})
    assert first.status_code == 422
    missing = first.json()["detail"]["unresolvedFields"]
    assert {field["value"] for field in missing} == {"invoiceNumber", "invoiceDate", "taxableAmount"}
    assert "invoice_number" not in str(missing)

    choices = {"invoiceNumber": "Record Ref", "taxableAmount": "Net Value", "invoiceDate": "When"}
    clarified = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        data={"column_choices": json.dumps(choices)},
        files={"file": ("custom.csv", initial, "text/csv")})
    assert clarified.status_code == 200, clarified.text
    profile = db.query(models.GSTR1ClientFormatProfile).filter_by(client_gstin_id=world["gstin"].id).one()
    assert profile.confirmed_columns["Record Ref"] == "invoice_number"

    changed_layout = b"When,Extra Note,Net Value,Record Ref\n16/09/2026,added column,2000,INV-NEW-2\n"
    reused = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09", headers=headers,
        files={"file": ("custom.csv", changed_layout, "text/csv")})
    assert reused.status_code == 200, reused.text
    assert reused.json()["recordsProcessed"] == 1
    db.refresh(profile)
    profiles = db.query(models.GSTR1ClientFormatProfile).filter_by(client_gstin_id=world["gstin"].id).all()
    assert len(profiles) == 2
    assert all(item.import_count == 1 for item in profiles)


def test_firm_rate_reference_is_owner_managed_effective_dated_and_explained(client, login, db, two_firms):
    world = two_firms["a"]
    other = two_firms["b"]
    db.add(models.GSTR1RuleDefinition(id="test-b2cl-rate-reference", rule_key="B2CL_THRESHOLD", version="TEST_THRESHOLD",
        definition={"threshold_inr": "100000"}, source_url="https://tutorial.gst.gov.in/",
        effective_from=date(2024, 8, 1), effective_to=None, verified_at=datetime.utcnow()))
    db.commit()
    owner_headers = login(world["users"]["owner"].email)
    associate_headers = login(world["users"]["associate"].email)
    reference = {"version": "firm-rates-2026-09", "effective_from": "2026-01-01", "effective_to": None,
        "source_url": "https://cbic.gov.in/example-rate-notification",
        "rates_by_hsn": {"9983": ["12"]}}
    saved = client.post("/api/gstr1/rate-references", headers=owner_headers, json=reference)
    assert saved.status_code == 200, saved.text
    assert saved.json()["hsn_count"] == 1
    assert client.post("/api/gstr1/rate-references", headers=associate_headers, json={**reference, "version": "unauthorized"}).status_code == 403
    assert client.get("/api/gstr1/rate-references", headers=login(other["users"]["owner"].email)).json() == []

    csv = ("invoice_number,invoice_date,taxable_value,cgst,sgst,invoice_value,hsn_sac,gst_rate\n"
           "INV-RATE,2026-09-10,100000,9000,9000,118000,9983,18\n").encode()
    uploaded = client.post(f"/api/gstr1/gstins/{world['gstin'].id}/uploads?period=2026-09",
        headers=associate_headers, files={"file": ("sales.csv", csv, "text/csv")})
    assert uploaded.status_code == 200, uploaded.text
    workspace = client.get(f"/api/gstr1/gstins/{world['gstin'].id}/returns/2026-09", headers=associate_headers).json()
    rate_issue = next(issue for issue in workspace["invoices"][0]["issues"] if issue["code"] == "GST_RATE_REFERENCE_MISMATCH")
    assert "invoice rate 18%" in rate_issue["message"]
    assert "reference rate option(s) 12%" in rate_issue["message"]
    assert "₹12,000.00" in rate_issue["message"]
    assert "not a definitive rate determination" in rate_issue["message"]
    assert workspace["gstRateReference"]["applicable"] is True
