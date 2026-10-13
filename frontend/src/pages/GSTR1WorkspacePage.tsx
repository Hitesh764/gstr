import { useEffect, useMemo, useState } from "react";
import { approveGstr1Return, decideGstr1Issue, downloadGstr1UploadSource, exportApprovedGstr1, exportDraftGstr1, exportDraftGstr1Workbook, exportGstr1AnnexurePdf, exportGstr1AnnexureReport, exportGstr1AnnexureWorkbook, getGstr1Checklist, getGstr1Return, getGstr1UploadVersions, listClients, reconcileGstr1Books, recordGstr1DocumentsIssued, reviewGstr1DocumentsIssued, reviewGstr1Invoice, saveGstr1RateReference, submitGstr1ForPartnerReview, uploadGstr1SalesRegister } from "../api/client";
import type { Client } from "../types";
import { useAuth } from "../auth/AuthContext";

type ReturnData = {
  id: string; period: string; status: string; rule_version: string;
  upload?: { version: number; filename: string; row_count: number; sha256: string } | null;
  summary: { invoice_count: number; issue_count: number; open_issues: number; categories: Record<string, {count:number; taxable_value:number; tax:number}> };
  gst_rate_reference: {configured:boolean; applicable:boolean; rule_versions:string[]; firm_references:Array<{version:string;source_url:string;effective_from:string;effective_to?:string;hsn_count:number}>; detail:string};
  reconciliation?: {filename:string; compared_at:string; books_invoice_count:number; preparation_invoice_count:number; books_totals:Record<string,number>; preparation_totals:Record<string,number>; totals_difference_books_minus_preparation:Record<string,number>; missing_from_preparation:Array<{invoice_number:string;customer_name?:string;taxable_value:number;invoice_value:number}>; not_in_books_register:Array<{invoice_number:string;customer_name?:string;taxable_value:number;invoice_value:number}>; amount_mismatches:Array<{invoice_number:string;books_taxable:number;preparation_taxable:number;books_tax:number;preparation_tax:number}>; duplicate_books_rows:Array<{invoice_number:string;reason:string}>; cancelled_in_books_but_active_in_preparation:Array<{invoice_number:string;message:string}>} | null;
  historical_trend?: {periods:Array<{period:string;invoice_count:number;taxable_value:number;b2b_share:number}>; anomaly_hints:string[]; basis:string};
  hsn_summary: Record<string, {hsn_sac:string; description?:string|null; reporting_group:string; gst_rate?:number|null; uqc?:string|null; invoice_count:number; record_count:number; quantity:number; taxable_value:number; cgst:number; sgst:number; igst:number; cess:number}>;
  annexure_report: {period_label:string; note:string; rows:Array<{particulars:string;records:number;taxable_amount:number;cgst:number;sgst:number;igst:number;cess:number;export_sez:number;igst_export:number}>; totals:{records:number;taxable_amount:number;cgst:number;sgst:number;igst:number;cess:number;export_sez:number;igst_export:number}};
  upload_issues: Array<{row_number?:number; code:string; severity:string; message:string; status:string}>;
  documents_issued: Array<{id:string; document_type:string; series_from:string; series_to:string; issued_count:number; cancelled_count:number; net_issued:number; version:number; prepared_by:string; reviewed_by?:string; reviewed_at?:string}>;
  invoices: Array<{id:string; row_number:number; invoice_number:string; invoice_date:string; customer_name?:string; customer_gstin?:string; place_of_supply?:string; invoice_value:number; taxable_value:number; cgst:number; sgst:number; igst:number; cess:number; gst_rate?:number|null; tax_check?:{rate:number;expected_gst:number;reported_gst:number;difference:number}|null; hsn_sac?:string; description?:string; original_invoice_number?:string; note_relationship?:{status:string;reported_status:string;original?:{invoice_number:string;invoice_date:string;invoice_value:number;taxable_value:number;tax:number;source:string;cumulative_credit_notes?:number;remaining_taxable_value?:number}}|null; invoice_status:string; document_type:string; supply_type:string; classification:string; classification_reason:string; status:string; issues:Array<{id:string; code:string; severity:string; message:string; status:string; review_decision?:string; review_note?:string}>}>;
};

const money = (amount: number) => new Intl.NumberFormat("en-IN", { style:"currency", currency:"INR", maximumFractionDigits:0 }).format(amount);
const annexureMoney = (amount: number) => amount === 0 ? "–" : new Intl.NumberFormat("en-IN", {style:"currency", currency:"INR", minimumFractionDigits:2, maximumFractionDigits:2}).format(amount);

export default function GSTR1WorkspacePage() {
  const { user } = useAuth();
  const canManageReview = user?.role === "MANAGER" || user?.role === "OWNER";
  const canPartnerApprove = user?.role === "PARTNER" || user?.role === "OWNER";
  const canManageRateReference = user?.role === "OWNER";
  const [clients, setClients] = useState<Client[]>([]);
  const [gstinId, setGstinId] = useState("");
  const [period, setPeriod] = useState(() => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}`; });
  const [data, setData] = useState<ReturnData | null>(null);
  const [versions, setVersions] = useState<Array<{id:string; period:string; version:number; is_current:boolean; original_filename:string; row_count:number; issue_count:number; uploaded_at:string}>>([]);
  const [checklist, setChecklist] = useState<{readiness:string; ready_for_filing:boolean; filed:boolean; checks:Array<{key:string;label:string;status:string;detail:string}>} | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [uploadFieldKey, setUploadFieldKey] = useState(0);
  const [ambiguousDateColumn, setAmbiguousDateColumn] = useState("");
  const [dateMeaning, setDateMeaning] = useState("");
  const [unresolvedFields, setUnresolvedFields] = useState<Array<{value:string;label:string;columns:string[]}>>([]);
  const [columnChoices, setColumnChoices] = useState<Record<string,string>>({});
  const [businessConflict, setBusinessConflict] = useState<{field:string;label:string;columns:Array<{name:string;examples:string[]}>;affectedRowCount:number;sampleRows:Array<{row_number:number;values:Record<string,string>}>} | null>(null);
  const [booksFile, setBooksFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [docType, setDocType] = useState("INVOICE");
  const [docFrom, setDocFrom] = useState("");
  const [docTo, setDocTo] = useState("");
  const [docIssued, setDocIssued] = useState("0");
  const [docCancelled, setDocCancelled] = useState("0");
  const [queueView, setQueueView] = useState<"attention" | "all" | "reviewed">("attention");
  const [queueIssue, setQueueIssue] = useState<string[] | null>(null);
  const [queueClassification, setQueueClassification] = useState<"B2B" | "B2C" | null>(null);
  const [invoiceSearch, setInvoiceSearch] = useState("");
  const [issueNotes, setIssueNotes] = useState<Record<string, string>>({});
  const [clientQuery, setClientQuery] = useState("");
  const [rateVersion, setRateVersion] = useState("");
  const [rateEffectiveFrom, setRateEffectiveFrom] = useState("");
  const [rateEffectiveTo, setRateEffectiveTo] = useState("");
  const [rateSourceUrl, setRateSourceUrl] = useState("");
  const [rateMapText, setRateMapText] = useState('{\n  "9983": ["12", "18"]\n}');
  const [annexureReportType, setAnnexureReportType] = useState<"sales"|"b2b"|"b2c"|"exports"|"notes"|"hsn_b2b"|"hsn_summary">("b2b");

  useEffect(() => { listClients().then(setClients).catch((e) => setError(e?.response?.data?.detail || "Could not load client registrations.")); }, []);
  const registrations = useMemo(() => clients.flatMap((client) => client.gstins.map((gstin) => ({...gstin, clientName: client.name}))), [clients]);
  const documentCountsReady = !!data && (data.period < "2025-05" || data.summary.invoice_count === 0 ||
    (data.documents_issued.length > 0 && data.documents_issued.every((row) => !!row.reviewed_by)));
  const readyForPartnerReview = !!data && data.summary.open_issues === 0 &&
    data.invoices.length > 0 && data.invoices.every((row) => row.status === "REVIEWED") && documentCountsReady;
  const visibleInvoices = useMemo(() => {
    if (!data) return [];
    const query = invoiceSearch.trim().toLowerCase();
    return data.invoices.filter((row) => {
      const matchesView = queueView === "all" || (queueView === "reviewed" ? row.status === "REVIEWED" : row.issues.some((issue) => issue.status === "OPEN"));
      const matchesIssue = queueView !== "attention" || !queueIssue || row.issues.some((issue) => queueIssue.includes(issue.code) && issue.status === "OPEN");
      const matchesClassification = !queueClassification || (queueClassification === "B2B" ? row.classification === "B2B" : ["B2CL", "B2CS"].includes(row.classification));
      const matchesSearch = !query || [row.invoice_number, row.customer_name, row.customer_gstin, row.classification].some((value) => value?.toLowerCase().includes(query));
      return matchesView && matchesIssue && matchesClassification && matchesSearch;
    });
  }, [data, queueView, queueIssue, queueClassification, invoiceSearch]);
  const attentionInvoiceCount = data?.invoices.filter((row) => row.issues.some((issue) => issue.status === "OPEN")).length || 0;
  const controlBreakdown = useMemo(() => {
    const groups = new Map<string, {documentType:string; category:string; count:number; taxable:number; tax:number; attention:number}>();
    for (const row of data?.invoices || []) {
      const key = `${row.document_type || "INVOICE"}|${row.classification || "UNCLASSIFIED"}`;
      const group = groups.get(key) || {documentType:row.document_type || "INVOICE", category:row.classification || "UNCLASSIFIED", count:0, taxable:0, tax:0, attention:0};
      group.count += 1; group.taxable += row.taxable_value; group.tax += row.cgst + row.sgst + row.igst + row.cess;
      if (row.issues.some((issue) => issue.status === "OPEN")) group.attention += 1;
      groups.set(key, group);
    }
    const order = ["INVOICE", "EXPORT", "SEZ", "DEEMED_EXPORT", "CREDIT_NOTE", "DEBIT_NOTE", "OTHER"];
    return [...groups.values()].sort((a,b) => order.indexOf(a.documentType) - order.indexOf(b.documentType) || a.category.localeCompare(b.category));
  }, [data?.invoices]);
  const breakdownTotals = useMemo(() => controlBreakdown.reduce((total,row) => ({count:total.count+row.count, taxable:total.taxable+row.taxable, tax:total.tax+row.tax, attention:total.attention+row.attention}), {count:0,taxable:0,tax:0,attention:0}), [controlBreakdown]);
  const categoryCount = (categories:string[]) => (data?.invoices || []).filter((row) => categories.includes(row.classification)).length;
  const countInvoicesWithIssues = (codes: string[]) => data?.invoices.filter((row) => row.issues.some((issue) => issue.status === "OPEN" && codes.includes(issue.code))).length || 0;
  function showQueue(issueCodes: string[] | null = null) {
    setQueueView("attention");
    setQueueIssue(issueCodes);
    setQueueClassification(null);
    requestAnimationFrame(() => Array.from(document.querySelectorAll("section")).find((section) => section.textContent?.includes("Accountant review queue"))?.scrollIntoView({behavior:"smooth", block:"start"}));
  }
  function showClassification(classification: "B2B" | "B2C") {
    setQueueView("all");
    setQueueIssue(null);
    setQueueClassification(classification);
    requestAnimationFrame(() => Array.from(document.querySelectorAll("section")).find((section) => section.textContent?.includes("Accountant review queue"))?.scrollIntoView({behavior:"smooth", block:"start"}));
  }

  async function load() {
    setError(""); setNotice(""); setData(null);
    if (!gstinId || !period) return;
    try {
      const result = await getGstr1Return(gstinId, period);
      const [history, checks] = await Promise.all([getGstr1UploadVersions(gstinId, period), getGstr1Checklist(result.id)]);
      setData(result); setVersions(history); setChecklist(checks);
    }
    catch (e: any) {
      if (e?.response?.status === 404) return;
      const detail = e?.response?.data?.detail;
      setError(typeof detail === "string" ? detail : e?.response?.status
        ? `Could not load GSTR-1 workspace (server returned ${e.response.status}).`
        : "Could not reach the GSTR-1 service. Check that the backend is running, then retry.");
    }
  }
  useEffect(() => { void load(); }, [gstinId, period]);

  async function upload() {
    if (!gstinId || !file) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const businessChoices = businessConflict && columnChoices[businessConflict.field]
        ? {[businessConflict.field]: columnChoices[businessConflict.field]} : undefined;
      const requiredFieldChoices = businessConflict ? {} : columnChoices;
      const result = await uploadGstr1SalesRegister(gstinId, period, file, dateMeaning || undefined, ambiguousDateColumn || undefined, requiredFieldChoices, businessChoices);
      setFile(null); setUploadFieldKey(current=>current+1); setAmbiguousDateColumn(""); setDateMeaning(""); setUnresolvedFields([]); setColumnChoices({}); setBusinessConflict(null); await load();
      setNotice(result.message);
    } catch (e: any) {
      const detail = e?.response?.data?.detail;
      if (detail?.ambiguousDate?.column) {
        setAmbiguousDateColumn(detail.ambiguousDate.column);
        setDateMeaning(""); setError(detail.message || "Please clarify the date column to continue.");
      } else if (detail?.unresolvedFields?.length) {
        setUnresolvedFields(detail.unresolvedFields); setColumnChoices({});
        setError(detail.message || "We couldn't identify important sales information in this file.");
      } else if (detail?.businessConflict) {
        setBusinessConflict(detail.businessConflict); setColumnChoices({});
        setError(detail.message || "Some rows have different values for the same sales detail.");
      } else setError(typeof detail === "string" ? detail : detail?.message || "We couldn't process this sales register. Please check the file and try again.");
    }
    finally { setBusy(false); }
  }

  async function reconcileBooks() {
    if (!data || !booksFile) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await reconcileGstr1Books(data.id, booksFile);
      setBooksFile(null); setNotice("Books comparison saved. Review missing, extra and amount-mismatch invoices below."); await load();
    } catch (e:any) { setError(typeof e?.response?.data?.detail === "string" ? e.response.data.detail : JSON.stringify(e?.response?.data?.detail || "Could not reconcile the books register.")); }
    finally { setBusy(false); }
  }

  async function review(invoiceId: string, decision: "reviewed" | "needs_review") {
    setBusy(true); setError("");
    try { await reviewGstr1Invoice(invoiceId, decision); await load(); }
    catch (e:any) { setError(e?.response?.data?.detail || "Could not record review."); }
    finally { setBusy(false); }
  }

  async function decideIssue(issueId: string, decision: "ACCEPT_VALID" | "IGNORE" | "NEEDS_CORRECTION") {
    setBusy(true); setError(""); setNotice("");
    try {
      await decideGstr1Issue(issueId, decision, issueNotes[issueId]);
      setIssueNotes((current) => { const next = {...current}; delete next[issueId]; return next; });
      setNotice(decision === "NEEDS_CORRECTION" ? "Correction request recorded. This check stays open until corrected source data is uploaded." : "Review decision recorded.");
      await load();
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not record the exception decision."); }
    finally { setBusy(false); }
  }

  function prepareClientQuery() {
    if (!data) return;
    const open = data.invoices.flatMap((row) => row.issues.filter((issue) => issue.status === "OPEN").map((issue) => ({invoice: row.invoice_number, issue})));
    const grouped = new Map<string, string[]>();
    for (const item of open) grouped.set(issueTitle(item.issue.code), [...(grouped.get(issueTitle(item.issue.code)) || []), item.invoice]);
    const lines = [`Please help us resolve these items for the ${data.period} GSTR-1 preparation:`, ""];
    for (const [title, invoices] of grouped) lines.push(`${title}: ${[...new Set(invoices)].join(", ")}`);
    if (data.upload_issues.length) lines.push(`Please correct or provide valid details for source rows: ${data.upload_issues.map((item) => item.row_number ?? "unknown row").join(", ")}.`);
    setClientQuery(lines.join("\n"));
  }

  async function copyClientQuery() {
    try { await navigator.clipboard.writeText(clientQuery); setNotice("Client query copied. Review it before sending."); }
    catch { setError("Clipboard access is unavailable. Select and copy the query text manually."); }
  }

  async function saveDocumentCounts() {
    if (!data || !docFrom.trim() || !docTo.trim()) return;
    setBusy(true); setError("");
    try {
      await recordGstr1DocumentsIssued(data.id, {document_type:docType, series_from:docFrom, series_to:docTo,
        issued_count:Number(docIssued), cancelled_count:Number(docCancelled)});
      setNotice("Document count saved as a new version. A different manager must review it.");
      setDocFrom(""); setDocTo(""); setDocIssued("0"); setDocCancelled("0"); await load();
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not save document counts."); }
    finally { setBusy(false); }
  }

  async function saveRateReference() {
    setError(""); setNotice("");
    let rates_by_hsn: Record<string, string | string[]>;
    try { rates_by_hsn = JSON.parse(rateMapText); }
    catch { setError("Enter the HSN/SAC rate list as valid JSON, for example {\"9983\":[\"12\",\"18\"]}."); return; }
    try {
      setBusy(true);
      await saveGstr1RateReference({version:rateVersion,effective_from:rateEffectiveFrom,effective_to:rateEffectiveTo || undefined,source_url:rateSourceUrl,rates_by_hsn});
      setNotice("GST rate reference saved. New uploads use the effective-dated reference; existing invoices keep their original review results until re-imported.");
      setRateVersion(""); setRateEffectiveFrom(""); setRateEffectiveTo(""); setRateSourceUrl("");
      if (gstinId && period) await load();
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not save the GST rate reference."); }
    finally { setBusy(false); }
  }

  async function reviewDocumentCount(id: string) {
    setBusy(true); setError("");
    try { await reviewGstr1DocumentsIssued(id); await load(); }
    catch (e:any) { setError(e?.response?.data?.detail || "Could not review document counts."); }
    finally { setBusy(false); }
  }

  async function downloadDraft() {
    if (!data) return;
    try {
      const draft = await exportDraftGstr1(gstinId, period);
      const blob = new Blob([JSON.stringify(draft, null, 2)], {type:"application/json"});
      const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `GSTR1-${data.period}-draft.json`; link.click(); URL.revokeObjectURL(url);
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not generate the draft export."); }
  }

  async function advanceReview(action: "submit" | "approve") {
    if (!data) return;
    setBusy(true); setError(""); setNotice("");
    try {
      if (action === "submit") await submitGstr1ForPartnerReview(data.id);
      else await approveGstr1Return(data.id);
      setNotice(action === "submit" ? "Manager review recorded. The return is awaiting partner approval." : "Partner approval recorded. The preparation data is approved for export, not filed.");
      await load();
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not update the review status."); }
    finally { setBusy(false); }
  }

  async function downloadApproved() {
    if (!data) return;
    try {
      const result = await exportApprovedGstr1(data.id);
      const blob = new Blob([JSON.stringify(result, null, 2)], {type:"application/json"});
      const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `GSTR1-${data.period}-approved-preparation.json`; link.click(); URL.revokeObjectURL(url);
    } catch (e:any) { setError(e?.response?.data?.detail || "Approved export is not available."); }
  }

  async function downloadWorkbook() {
    try {
      const blob = await exportDraftGstr1Workbook(gstinId, period);
      const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `GSTR1-${period}-draft.xlsx`; link.click(); URL.revokeObjectURL(url);
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not generate the draft workbook."); }
  }

  async function downloadAnnexure() {
    try {
      const blob = await exportGstr1AnnexureWorkbook(gstinId, period);
      const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `GSTR1-${period}-annexure-summary.xlsx`; link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not generate the annexure report."); }
  }

  async function downloadAnnexurePdf() {
    try {
      const blob = await exportGstr1AnnexurePdf(gstinId, period);
      const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = `GSTR1-${period}-annexure-summary.pdf`; link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not generate the printable report."); }
  }

  async function downloadAnnexureReport() {
    try {
      const blob = await exportGstr1AnnexureReport(gstinId, period, annexureReportType);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "GSTR1-" + period + "-" + annexureReportType + ".xlsx";
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not download the selected Annexure report."); }
  }


  async function downloadSource(uploadId: string, filename: string) {
    try {
      const blob = await downloadGstr1UploadSource(uploadId);
      const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = filename; link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e:any) { setError(e?.response?.data?.detail || "Could not download the original source register."); }
  }

  const hsnSummaryRows = Object.values(data?.hsn_summary || {});
  const hsnSummaryTotals = hsnSummaryRows.reduce((totals, row) => ({
    taxable: totals.taxable + row.taxable_value, cgst: totals.cgst + row.cgst,
    sgst: totals.sgst + row.sgst, igst: totals.igst + row.igst, cess: totals.cess + row.cess,
  }), {taxable:0, cgst:0, sgst:0, igst:0, cess:0});

  return <main className="mx-auto max-w-7xl px-4 py-8">
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div><p className="text-xs font-semibold uppercase tracking-[0.2em] text-teal-700">Outward supplies</p><h1 className="mt-2 text-3xl font-semibold text-slate-900">GSTR-1 Control Center</h1><p className="mt-2 max-w-3xl text-slate-600">Prepare a period-wise return from the sales register. Classifications and checks are system suggestions for accountant review.</p></div>
      {data && <div className="flex gap-2"><button onClick={()=>void downloadDraft()} className="rounded-lg border border-slate-300 bg-white px-4 py-2 font-medium text-slate-700">Download draft JSON</button>{data.status !== "APPROVED" && <button onClick={()=>void downloadWorkbook()} className="rounded-lg border border-slate-300 bg-white px-4 py-2 font-medium text-slate-700">Download draft workbook</button>}{data.status === "APPROVED" && canPartnerApprove && <button onClick={()=>void downloadApproved()} className="rounded-lg bg-teal-700 px-4 py-2 font-semibold text-white">Download approved preparation</button>}</div>}
    </div>

    <section className="mt-7 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <h2 className="font-semibold text-slate-900">Choose registration and period</h2>
      <div className="mt-4 grid gap-4 md:grid-cols-[1fr_180px_1fr_auto] md:items-end">
        <label className="text-sm font-medium text-slate-700">Client registration<select value={gstinId} onChange={e=>setGstinId(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5">
          <option value="">Select GSTIN</option>{registrations.map(g=><option key={g.id} value={g.id}>{g.clientName} · {g.gstin}</option>)}
        </select></label>
        <label className="text-sm font-medium text-slate-700">Tax period<input type="month" value={period} onChange={e=>setPeriod(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2.5" /></label>
        <label className="text-sm font-medium text-slate-700">Upload sales register (Excel/CSV)<input key={uploadFieldKey} type="file" accept=".csv,.xlsx,.xls" onChange={e=>{setFile(e.target.files?.[0] || null);setAmbiguousDateColumn("");setDateMeaning("");setUnresolvedFields([]);setColumnChoices({});setBusinessConflict(null);setError("");}} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2" /></label>
        <button disabled={!gstinId || !file || busy || (!!ambiguousDateColumn && !dateMeaning) || unresolvedFields.some((field)=>!columnChoices[field.value]) || (!!businessConflict && !columnChoices[businessConflict.field])} onClick={()=>void upload()} className="rounded-lg bg-teal-700 px-5 py-2.5 font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50">{busy && file ? "Processing file…" : ambiguousDateColumn || unresolvedFields.length || businessConflict ? "Confirm & process" : "Upload & continue"}</button>
      </div>
      <p className="mt-3 text-xs text-slate-500">Upload your sales register in Excel or CSV format. Common invoice, customer, date and tax headings are recognized automatically. The original file is securely retained for audit.</p>
      {ambiguousDateColumn && <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4"><label className="block text-sm font-medium text-amber-950">We found a “{ambiguousDateColumn}” column. Which date does it contain?<select value={dateMeaning} onChange={event=>setDateMeaning(event.target.value)} className="mt-2 block w-full max-w-sm rounded-lg border border-amber-300 bg-white px-3 py-2"><option value="">Choose a date type</option><option value="invoiceDate">Invoice date</option><option value="paymentDate">Payment date</option><option value="dueDate">Due date</option><option value="otherDate">Another date</option></select></label></div>}
      {unresolvedFields.length > 0 && <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4"><p className="text-sm font-semibold text-amber-950">Please identify the missing sales information using your file's headings.</p><div className="mt-3 grid gap-3 sm:grid-cols-2">{unresolvedFields.map(field=><label key={field.value} className="text-sm font-medium text-amber-950">{field.label}<select value={columnChoices[field.value] || ""} onChange={event=>setColumnChoices(current=>({...current,[field.value]:event.target.value}))} className="mt-1 block w-full rounded-lg border border-amber-300 bg-white px-3 py-2"><option value="">Choose a column</option>{field.columns.map(column=><option key={column} value={column}>{column}</option>)}</select></label>)}</div></div>}
      {businessConflict && <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4" role="region" aria-label={`${businessConflict.label} conflict review`}><div className="flex flex-wrap items-start justify-between gap-2"><div><h3 className="text-sm font-semibold text-amber-950">Review {businessConflict.label.toLowerCase()} values</h3><p className="mt-1 text-sm text-amber-900">{businessConflict.affectedRowCount.toLocaleString()} rows contain different values in these columns. Compare the examples with the source invoices, then choose which column to use for those rows.</p></div><button type="button" onClick={()=>{setFile(null);setUploadFieldKey(current=>current+1);setBusinessConflict(null);setColumnChoices({});setError("");}} className="text-sm font-medium text-amber-900 underline">Cancel import</button></div><label className="mt-3 block max-w-xl text-sm font-medium text-amber-950">Use this column for {businessConflict.label.toLowerCase()}<select value={columnChoices[businessConflict.field] || ""} onChange={event=>setColumnChoices(current=>({...current,[businessConflict.field]:event.target.value}))} className="mt-1 block w-full rounded-lg border border-amber-300 bg-white px-3 py-2"><option value="">Choose after reviewing examples</option>{businessConflict.columns.map(column=><option key={column.name} value={column.name}>{column.name}</option>)}</select></label><p className="mt-2 text-xs text-amber-900">Your selection will be applied to {businessConflict.affectedRowCount.toLocaleString()} conflicting rows. The original file and both source values are retained for audit.</p><div className="mt-3 overflow-x-auto rounded-md border border-amber-200 bg-white"><table className="min-w-full text-left text-xs"><thead className="bg-amber-100 text-amber-950"><tr><th className="px-3 py-2">Source row</th>{businessConflict.columns.map(column=><th key={column.name} className="px-3 py-2">{column.name}</th>)}</tr></thead><tbody className="divide-y divide-amber-100">{businessConflict.sampleRows.map(row=><tr key={row.row_number}><td className="px-3 py-2 font-medium">{row.row_number}</td>{businessConflict.columns.map(column=><td key={column.name} className="px-3 py-2">{row.values[column.name] || "—"}</td>)}</tr>)}</tbody></table></div></div>}
      {busy && file && <div role="status" className="mt-4 flex items-center gap-3 rounded-lg border border-teal-100 bg-teal-50 p-3 text-sm text-teal-900"><span className="h-4 w-4 animate-spin rounded-full border-2 border-teal-700 border-r-transparent"/><span><strong>Processing your sales register…</strong><span className="ml-1 text-teal-800">Reading invoices and checking the supplied GST details.</span></span></div>}
      {error && <p role="alert" className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
      {notice && <p role="status" className="mt-4 rounded-lg bg-emerald-50 p-3 text-sm text-emerald-800">{notice}</p>}
    </section>

    {!gstinId && <div className="mt-7 rounded-xl border border-dashed border-slate-300 p-10 text-center text-slate-500">Select a client GSTIN to view its GSTR-1 preparation workspace.</div>}
    {gstinId && data && <>
      <div className="mt-7 flex flex-wrap items-center justify-between gap-3"><div><h2 className="text-xl font-semibold">{data.period} return preparation</h2><p className="mt-1 text-sm text-slate-500">{data.status.replaceAll("_", " ")} · Rule set {data.rule_version} · Upload v{data.upload?.version} ({data.upload?.filename})</p></div><div className="flex flex-wrap items-center gap-2"><span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-bold tracking-wide text-amber-800">{data.status === "APPROVED" ? "PARTNER APPROVED — NOT FILED" : `${data.status.replaceAll("_", " ")} — NOT FILED`}</span>{data.status === "DRAFT" && canManageReview && <button disabled={busy || !readyForPartnerReview} onClick={()=>void advanceReview("submit")} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold disabled:opacity-50">Submit to partner</button>}{data.status === "IN_REVIEW" && canPartnerApprove && <button disabled={busy} onClick={()=>void advanceReview("approve")} className="rounded-lg bg-teal-700 px-3 py-2 text-xs font-semibold text-white">Partner approve</button>}</div></div>
      {versions.length > 0 && <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-600"><span className="font-semibold">Register versions:</span>{versions.map(version=><div key={version.id} className={`flex items-center gap-2 rounded-full px-2 py-1 ${version.is_current?"bg-teal-100 text-teal-900":"bg-slate-100"}`}><span>v{version.version} · {version.row_count} rows · {version.issue_count} flags{version.is_current?" · Current":""}</span><button onClick={()=>void downloadSource(version.id, version.original_filename)} className="font-semibold underline decoration-current underline-offset-2">Download source</button></div>)}</div>}
      {checklist && <section className="mt-5 rounded-2xl border border-amber-200 bg-white p-5 shadow-sm"><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="font-semibold">Preparation readiness checklist</h3><p className="mt-1 text-sm text-slate-600">Partner approval is recorded separately from readiness and filing.</p></div><span className={`rounded-full px-3 py-1 text-xs font-bold ${checklist.ready_for_filing?"bg-emerald-100 text-emerald-800":"bg-amber-100 text-amber-900"}`}>{checklist.readiness.replaceAll("_", " ")}</span></div><ul className="mt-4 grid gap-3 md:grid-cols-2">{checklist.checks.map(item=><li key={item.key} className="rounded-lg border border-slate-100 p-3"><div className="flex items-center justify-between gap-2"><span className="text-sm font-medium text-slate-800">{item.label}</span><span className={`text-[10px] font-bold uppercase tracking-wide ${item.status==="PASS"?"text-emerald-700":item.status==="NOT_APPLICABLE"?"text-slate-500":"text-amber-800"}`}>{item.status.replaceAll("_", " ")}</span></div><p className="mt-1 text-xs leading-5 text-slate-500">{item.detail}</p></li>)}</ul><p className="mt-4 text-xs font-semibold text-slate-600">Filing status: NOT FILED. This application does not submit returns.</p></section>}
      <section className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3"><div><p className="text-xs font-semibold uppercase tracking-wide text-teal-700">{periodLabel(data.period)} GSTR-1</p><h3 className="mt-1 text-lg font-semibold text-slate-900">Control center</h3><p className="mt-1 text-sm text-slate-500">A clear view of document types, GSTR-1 reporting categories and checks in the current register.</p></div><span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-700">{data.summary.open_issues} open checks · records may have more than one</span></div>
        <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Metric title="Total documents" value={data.summary.invoice_count} hint="Invoices, notes and other uploaded documents"/>
          <Metric title="Records needing attention" value={attentionInvoiceCount} warn={attentionInvoiceCount>0} onClick={()=>showQueue()} hint="Each record counted once, even if it has several open checks"/>
          <Metric title="B2B supplies" value={categoryCount(["B2B"])} onClick={()=>showClassification("B2B")} hint="Valid recipient GSTIN format; status may need external verification"/>
          <Metric title="B2C supplies" value={categoryCount(["B2CL","B2CS"])} onClick={()=>showClassification("B2C")} hint="Recipient GSTIN not supplied"/>
          <Metric title="Needs classification" value={categoryCount(["UNCLASSIFIED","OTHER"])} warn={categoryCount(["UNCLASSIFIED","OTHER"])>0} onClick={()=>showQueue(["GSTIN_INVALID","DOCUMENT_TYPE_REQUIRES_REVIEW"])} hint="Check GSTIN or document type"/>
          <Metric title="Exports" value={categoryCount(["EXPORT","DEEMED_EXPORT"])} hint={`${categoryCount(["DEEMED_EXPORT"])} deemed export records`}/>
          <Metric title="SEZ supplies" value={categoryCount(["SEZ"])} hint="Review applicable SEZ reporting treatment"/>
          <Metric title="Credit notes" value={categoryCount(["CREDIT_NOTE"])} />
          <Metric title="Debit notes" value={categoryCount(["DEBIT_NOTE"])} />
          <Metric title="Possible duplicate documents" value={countInvoicesWithIssues(["POSSIBLE_DUPLICATE","POSSIBLE_FUZZY_DUPLICATE","POSSIBLE_DUPLICATE_NOTE"])} warn={countInvoicesWithIssues(["POSSIBLE_DUPLICATE","POSSIBLE_FUZZY_DUPLICATE","POSSIBLE_DUPLICATE_NOTE"])>0} onClick={()=>showQueue(["POSSIBLE_DUPLICATE","POSSIBLE_FUZZY_DUPLICATE","POSSIBLE_DUPLICATE_NOTE"])} hint="Invoices and notes remain in the register for review"/>
          <Metric title="GSTIN errors" value={countInvoicesWithIssues(["GSTIN_INVALID","CUSTOMER_GSTIN_DIFFERS_FROM_HISTORY","CUSTOMER_GSTIN_INCONSISTENT"])} warn={countInvoicesWithIssues(["GSTIN_INVALID","CUSTOMER_GSTIN_DIFFERS_FROM_HISTORY","CUSTOMER_GSTIN_INCONSISTENT"])>0} onClick={()=>showQueue(["GSTIN_INVALID","CUSTOMER_GSTIN_DIFFERS_FROM_HISTORY","CUSTOMER_GSTIN_INCONSISTENT"])} hint="View GSTIN flags"/>
          <Metric title="Tax calculation errors" value={countInvoicesWithIssues(["TAX_RATE_ARITHMETIC","TOTAL_MISMATCH","TAX_SPLIT_CONFLICT","PLACE_OF_SUPPLY_TAX_SPLIT"])} warn={countInvoicesWithIssues(["TAX_RATE_ARITHMETIC","TOTAL_MISMATCH","TAX_SPLIT_CONFLICT","PLACE_OF_SUPPLY_TAX_SPLIT"])>0} onClick={()=>showQueue(["TAX_RATE_ARITHMETIC","TOTAL_MISMATCH","TAX_SPLIT_CONFLICT","PLACE_OF_SUPPLY_TAX_SPLIT"])} hint="View tax amount and split checks"/>
          <Metric title="GST rate reference check" value={data.gst_rate_reference.applicable ? countInvoicesWithIssues(["GST_RATE_REFERENCE_MISMATCH","GST_RATE_REFERENCE_NOT_FOUND","GST_RATE_MISSING"]) : "Unavailable"} warn={!data.gst_rate_reference.applicable || countInvoicesWithIssues(["GST_RATE_REFERENCE_MISMATCH","GST_RATE_REFERENCE_NOT_FOUND","GST_RATE_MISSING"])>0} onClick={data.gst_rate_reference.applicable ? ()=>showQueue(["GST_RATE_REFERENCE_MISMATCH","GST_RATE_REFERENCE_NOT_FOUND","GST_RATE_MISSING"]) : undefined} hint={data.gst_rate_reference.applicable ? `${data.gst_rate_reference.rule_versions.join(", ")} · Review prompt only` : data.gst_rate_reference.detail}/>
          <Metric title="HSN/SAC issues" value={countInvoicesWithIssues(["HSN_MISSING","HSN_FORMAT_REVIEW","CUSTOMER_HSN_CHANGED_FROM_HISTORY"])} warn={countInvoicesWithIssues(["HSN_MISSING","HSN_FORMAT_REVIEW","CUSTOMER_HSN_CHANGED_FROM_HISTORY"])>0} onClick={()=>showQueue(["HSN_MISSING","HSN_FORMAT_REVIEW","CUSTOMER_HSN_CHANGED_FROM_HISTORY"])} hint="View HSN/SAC flags"/>
          <Metric title="Missing information" value={countInvoicesWithIssues(["NOTE_ORIGINAL_MISSING"]) + data.upload_issues.filter((issue)=>issue.code==="MISSING_INVOICE_NUMBER").length} warn={countInvoicesWithIssues(["NOTE_ORIGINAL_MISSING"])>0 || data.upload_issues.some((issue)=>issue.code==="MISSING_INVOICE_NUMBER")} onClick={()=>showQueue(["NOTE_ORIGINAL_MISSING"])} hint="View missing details"/>
          <Metric title="Books vs GSTR-1 difference" value={data.reconciliation ? moneyCompact(data.reconciliation.totals_difference_books_minus_preparation.taxable_value) : "Not compared"} warn={!!data.reconciliation && Object.values(data.reconciliation.totals_difference_books_minus_preparation).some((amount)=>Math.abs(amount)>0.01)} onClick={()=>Array.from(document.querySelectorAll("section")).find((section)=>section.textContent?.includes("Books comparison"))?.scrollIntoView({behavior:"smooth",block:"start"})} hint={data.reconciliation ? "Taxable value difference" : "Upload books register to compare"}/>
        </div>
        <div className="mt-5 border-t border-slate-100 pt-4"><div><h4 className="text-sm font-semibold text-slate-900">Document type and reporting category</h4><p className="mt-1 text-xs text-slate-500">Document type is read from the sales register. Reporting category is determined from the document, recipient GSTIN and supply details; for example, an invoice may be reported as an export.</p></div>
          <div className="mt-3 overflow-x-auto"><table className="min-w-full text-left text-sm"><thead className="text-xs uppercase text-slate-500"><tr><th className="py-2 pr-4">Document type</th><th className="py-2 pr-4">GSTR-1 category</th><th className="py-2 pr-4">Records</th><th className="py-2 pr-4">Taxable value</th><th className="py-2 pr-4">Total tax</th><th className="py-2 pr-4">Need attention</th></tr></thead><tbody className="divide-y divide-slate-100">{controlBreakdown.map((row)=><tr key={`${row.documentType}-${row.category}`}><td className="py-2 pr-4 font-medium text-slate-900">{documentTypeLabel(row.documentType)}</td><td className="py-2 pr-4">{reportingCategoryLabel(row.category)}</td><td className="py-2 pr-4">{row.count.toLocaleString("en-IN")}</td><td className="py-2 pr-4">{money(row.taxable)}</td><td className="py-2 pr-4">{money(row.tax)}</td><td className="py-2 pr-4">{row.attention ? <span className="font-semibold text-amber-800">{row.attention}</span> : "0"}</td></tr>)}</tbody><tfoot className="border-t border-slate-200 font-semibold text-slate-900"><tr><td className="py-2 pr-4" colSpan={2}>Total</td><td className="py-2 pr-4">{breakdownTotals.count.toLocaleString("en-IN")}</td><td className="py-2 pr-4">{money(breakdownTotals.taxable)}</td><td className="py-2 pr-4">{money(breakdownTotals.tax)}</td><td className="py-2 pr-4">{breakdownTotals.attention.toLocaleString("en-IN")}</td></tr></tfoot></table>{controlBreakdown.length===0 && <p className="py-5 text-sm text-slate-500">No imported document rows are available.</p>}</div>
        </div>
        {data.upload_issues.length>0 && <p className="mt-3 rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-800">{data.upload_issues.length} source {data.upload_issues.length===1?"row was":"rows were"} excluded from the import. See the excluded rows below.</p>}
      </section>
      <section className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><div className="flex flex-wrap items-end justify-between gap-3"><div><h3 className="font-semibold">Books comparison</h3><p className="mt-1 text-sm text-slate-500">Compare against a separate sales register from the books. This does not compare with GST Portal data.</p></div>{data.status==="DRAFT" && <div className="flex flex-wrap items-end gap-2"><label className="text-xs font-medium text-slate-600">Books register (CSV/XLSX)<input type="file" accept=".csv,.xlsx,.xls" onChange={(event)=>setBooksFile(event.target.files?.[0] || null)} className="mt-1 block max-w-xs rounded-lg border border-slate-300 px-2 py-2" /></label><button disabled={!booksFile || busy} onClick={()=>void reconcileBooks()} className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Compare registers</button></div>}</div>{data.reconciliation ? <><p className="mt-3 text-xs text-slate-500">Compared with {data.reconciliation.filename} · {data.reconciliation.books_invoice_count.toLocaleString("en-IN")} books rows · {data.reconciliation.preparation_invoice_count.toLocaleString("en-IN")} preparation rows</p><div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><Metric title="Books missing from preparation" value={data.reconciliation.missing_from_preparation.length} warn={data.reconciliation.missing_from_preparation.length>0}/><Metric title="Not found in books file" value={data.reconciliation.not_in_books_register.length} warn={data.reconciliation.not_in_books_register.length>0}/><Metric title="Amount mismatches" value={data.reconciliation.amount_mismatches.length} warn={data.reconciliation.amount_mismatches.length>0}/><Metric title="Repeated books rows" value={data.reconciliation.duplicate_books_rows.length} warn={data.reconciliation.duplicate_books_rows.length>0}/></div><p className="mt-3 text-sm text-slate-700">Taxable difference {money(data.reconciliation.totals_difference_books_minus_preparation.taxable_value)} · Tax difference {money(data.reconciliation.totals_difference_books_minus_preparation.tax)} · Invoice value difference {money(data.reconciliation.totals_difference_books_minus_preparation.invoice_value)}</p><details className="mt-3"><summary className="cursor-pointer text-sm font-semibold text-teal-800">View invoice differences</summary><ul className="mt-2 space-y-1 text-sm text-slate-700">{data.reconciliation.missing_from_preparation.map((item,index)=><li key={`missing-${index}`}>Missing from preparation: <strong>{item.invoice_number}</strong> · {money(item.invoice_value)}</li>)}{data.reconciliation.not_in_books_register.map((item,index)=><li key={`extra-${index}`}>Not in books file: <strong>{item.invoice_number}</strong> · {money(item.invoice_value)}</li>)}{data.reconciliation.amount_mismatches.map((item,index)=><li key={`mismatch-${index}`}>Amount mismatch: <strong>{item.invoice_number}</strong> · taxable {money(item.books_taxable)} (books) / {money(item.preparation_taxable)} (preparation)</li>)}</ul></details></> : <p className="mt-3 rounded-lg bg-slate-50 p-3 text-sm text-slate-600">Upload a separate books register to check missing invoices and compare values.</p>}</section>
      <section className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-2"><div><h3 className="font-semibold">Automatic HSN/SAC summary</h3><p className="mt-1 text-sm text-slate-500">Grouped from the processed invoice lines by HSN/SAC, reporting category, GST rate and unit. Each amount is added from those lines; credit notes reduce the totals.</p></div><span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-700">{Object.keys(data.hsn_summary).length} grouped rows</span></div>
        <div className="mt-4 overflow-x-auto"><table className="min-w-full text-left text-sm"><thead className="text-xs uppercase text-slate-500"><tr><th className="py-2 pr-4">HSN/SAC</th><th className="py-2 pr-4">Description</th><th className="py-2 pr-4">Category</th><th className="py-2 pr-4">Rate</th><th className="py-2 pr-4">Invoices</th><th className="py-2 pr-4">Quantity / unit</th><th className="py-2 pr-4">Taxable value</th><th className="py-2 pr-4">CGST</th><th className="py-2 pr-4">SGST</th><th className="py-2 pr-4">IGST</th><th className="py-2 pr-4">Cess</th><th className="py-2 pr-4">Total tax</th></tr></thead><tbody className="divide-y divide-slate-100">{Object.entries(data.hsn_summary).map(([key,row])=><tr key={key}><td className={`py-2 pr-4 font-medium ${row.hsn_sac==="MISSING"?"text-amber-800":"text-slate-900"}`}>{row.hsn_sac==="MISSING"?"Missing HSN/SAC":row.hsn_sac}</td><td className="py-2 pr-4">{row.description || "—"}</td><td className="py-2 pr-4">{row.reporting_group}</td><td className="py-2 pr-4">{row.gst_rate == null ? "Not supplied" : `${row.gst_rate}%`}</td><td className="py-2 pr-4">{row.invoice_count}</td><td className="py-2 pr-4">{row.quantity.toLocaleString("en-IN", {maximumFractionDigits:3})} {row.uqc || ""}</td><td className="py-2 pr-4">{annexureMoney(row.taxable_value)}</td><td className="py-2 pr-4">{annexureMoney(row.cgst)}</td><td className="py-2 pr-4">{annexureMoney(row.sgst)}</td><td className="py-2 pr-4">{annexureMoney(row.igst)}</td><td className="py-2 pr-4">{annexureMoney(row.cess)}</td><td className="py-2 pr-4 font-semibold">{annexureMoney(row.cgst+row.sgst+row.igst+row.cess)}</td></tr>)}</tbody>{hsnSummaryRows.length>0 && <tfoot className="border-t-2 border-slate-200 bg-slate-50 font-semibold"><tr><td className="py-2 pr-4" colSpan={4}>Monetary totals</td><td className="py-2 pr-4 text-slate-500" title="One invoice can appear in more than one HSN group">—</td><td className="py-2 pr-4 text-slate-500" title="Quantity units can differ">—</td><td className="py-2 pr-4">{annexureMoney(hsnSummaryTotals.taxable)}</td><td className="py-2 pr-4">{annexureMoney(hsnSummaryTotals.cgst)}</td><td className="py-2 pr-4">{annexureMoney(hsnSummaryTotals.sgst)}</td><td className="py-2 pr-4">{annexureMoney(hsnSummaryTotals.igst)}</td><td className="py-2 pr-4">{annexureMoney(hsnSummaryTotals.cess)}</td><td className="py-2 pr-4">{annexureMoney(hsnSummaryTotals.cgst+hsnSummaryTotals.sgst+hsnSummaryTotals.igst+hsnSummaryTotals.cess)}</td></tr></tfoot>}</table>{hsnSummaryRows.length===0 && <p className="py-8 text-center text-sm text-slate-500">No processed invoice lines are available for an HSN/SAC summary.</p>}</div>
        <p className="mt-3 text-xs text-slate-500">“Invoices” counts distinct invoice numbers within each group; quantities and tax amounts are summed from the uploaded invoice lines. A missing HSN/SAC remains visible so it can be reviewed.</p>
      </section>
      {data.upload_issues.length > 0 && <section className="mt-5 rounded-2xl border border-rose-200 bg-rose-50 p-5"><h3 className="font-semibold text-rose-900">Rows excluded from this preparation ({data.upload_issues.length})</h3><p className="mt-1 text-sm text-rose-800">These source rows were not imported. Correct the source register and upload a new version.</p><ul className="mt-3 space-y-2 text-sm text-rose-900">{data.upload_issues.map((issue,index)=><li key={`${issue.row_number}-${issue.code}-${index}`} className="rounded-lg border border-rose-100 bg-white/70 px-3 py-2"><strong>Row {issue.row_number ?? "—"} · {issueTitle(issue.code)}</strong><p className="mt-1">{issue.message}</p></li>)}</ul></section>}
      {period >= "2025-05" && data.summary.invoice_count > 0 && <section className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><div><h3 className="font-semibold">Documents issued · Table 13</h3><p className="mt-1 text-sm text-slate-500">Record each document series and its issued/cancelled counts. Each current count record needs an independent manager review before partner review.</p></div>
        {data.status === "DRAFT" && <div className="mt-4 grid gap-3 md:grid-cols-5"><label className="text-xs font-medium text-slate-600">Document type<select value={docType} onChange={e=>setDocType(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 bg-white px-2 py-2"><option>INVOICE</option><option>CREDIT_NOTE</option><option>DEBIT_NOTE</option><option>OTHER</option></select></label><label className="text-xs font-medium text-slate-600">Series from<input value={docFrom} onChange={e=>setDocFrom(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2" /></label><label className="text-xs font-medium text-slate-600">Series to<input value={docTo} onChange={e=>setDocTo(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2" /></label><label className="text-xs font-medium text-slate-600">Issued count<input type="number" min="0" value={docIssued} onChange={e=>setDocIssued(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2" /></label><label className="text-xs font-medium text-slate-600">Cancelled count<input type="number" min="0" value={docCancelled} onChange={e=>setDocCancelled(e.target.value)} className="mt-1 block w-full rounded border border-slate-300 px-2 py-2" /></label><div className="md:col-span-5"><button disabled={busy || !docFrom.trim() || !docTo.trim()} onClick={()=>void saveDocumentCounts()} className="rounded bg-slate-900 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Save document series</button></div></div>}
        {data.documents_issued.length > 0 ? <div className="mt-4 overflow-x-auto"><table className="min-w-full text-left text-sm"><thead className="text-xs uppercase text-slate-500"><tr><th className="py-2 pr-4">Type</th><th className="py-2 pr-4">Series</th><th className="py-2 pr-4">Issued</th><th className="py-2 pr-4">Cancelled</th><th className="py-2 pr-4">Net</th><th className="py-2 pr-4">Review</th></tr></thead><tbody className="divide-y divide-slate-100">{data.documents_issued.map(row=><tr key={row.id}><td className="py-2 pr-4">{row.document_type}</td><td className="py-2 pr-4">{row.series_from} – {row.series_to} · v{row.version}</td><td className="py-2 pr-4">{row.issued_count}</td><td className="py-2 pr-4">{row.cancelled_count}</td><td className="py-2 pr-4">{row.net_issued}</td><td className="py-2 pr-4">{row.reviewed_by ? <span className="text-emerald-700">Reviewed</span> : canManageReview ? <button disabled={busy || data.status!=="DRAFT"} onClick={()=>void reviewDocumentCount(row.id)} className="rounded border border-slate-300 px-2 py-1 text-xs">Review counts</button> : <span className="text-amber-700">Review pending</span>}</td></tr>)}</tbody></table></div> : <p className="mt-4 rounded bg-amber-50 p-3 text-sm text-amber-800">No document series counts recorded. This blocks partner review for the current period.</p>}
      </section>}
      <section className="mt-6 rounded-2xl border border-slate-200 bg-white shadow-sm"><div className="border-b border-slate-100 px-5 py-4"><h3 className="font-semibold">Review invoices</h3><p className="mt-1 text-sm text-slate-600">A warning means “please check this,” not “this is definitely wrong.” Compare it with the original invoice before deciding.</p><div className="mt-3 rounded-lg bg-sky-50 px-3 py-2 text-xs leading-5 text-sky-900"><strong>How to review:</strong> Read “What we found” → check the source invoice → choose <strong>Confirmed valid</strong> if the source is correct, or <strong>Needs correction</strong> if it is wrong. Your decision records the review; it does not edit the uploaded file.</div><div className="mt-4 flex flex-wrap items-center gap-2"><div className="flex rounded-lg bg-slate-100 p-1" aria-label="Filter transactions">{([ ["attention", `Needs review (${data.invoices.filter((row) => row.issues.some((issue) => issue.status === "OPEN")).length})`], ["all", `All invoices (${data.invoices.length})`], ["reviewed", `Reviewed (${data.invoices.filter((row) => row.status === "REVIEWED").length})`] ] as const).map(([view,label])=><button key={view} onClick={()=>{setQueueView(view);setQueueIssue(null);setQueueClassification(null);}} aria-pressed={queueView===view} className={`rounded-md px-3 py-2 text-xs font-semibold ${queueView===view?"bg-white text-slate-900 shadow-sm":"text-slate-600"}`}>{label}</button>)}</div>{queueClassification && <button onClick={()=>setQueueClassification(null)} className="rounded-full bg-teal-50 px-3 py-2 text-xs font-semibold text-teal-900">Showing {queueClassification} · Clear</button>}<input value={invoiceSearch} onChange={(event)=>setInvoiceSearch(event.target.value)} placeholder="Search invoice or customer" aria-label="Search invoice or customer" className="min-w-56 flex-1 rounded-lg border border-slate-300 px-3 py-2 text-sm" /></div></div>
        {visibleInvoices.length ? <div className="divide-y divide-slate-100">{visibleInvoices.map(row=><article key={row.id} className="px-5 py-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><div className="flex flex-wrap items-center gap-2"><h4 className="font-semibold text-slate-900">{row.invoice_number}</h4><span className="rounded-full bg-blue-50 px-2 py-1 text-xs font-semibold text-blue-800">{classificationLabel(row.classification)}</span><span className={`rounded-full px-2 py-1 text-xs font-semibold ${row.status==="REVIEWED"?"bg-emerald-50 text-emerald-800":"bg-slate-100 text-slate-700"}`}>{row.status==="REVIEWED"?"Reviewed":"Not reviewed"}</span></div><p className="mt-1 text-sm text-slate-600">{row.customer_name || "Customer not supplied"}{row.customer_gstin ? ` · ${row.customer_gstin}` : " · GSTIN not supplied"} · {row.invoice_date}</p><p className="mt-1 text-xs text-slate-500">System category: {row.classification_reason}</p><details className="mt-2"><summary className="cursor-pointer text-xs font-medium text-slate-600">Show tax and supply details</summary><p className="mt-2 text-xs text-slate-600">Taxable {money(row.taxable_value)} · Place of supply {row.place_of_supply || "not supplied"} · HSN/SAC {row.hsn_sac || "not supplied"} · GST rate {row.gst_rate == null ? "not supplied" : `${row.gst_rate}%`} · CGST {money(row.cgst)} · SGST {money(row.sgst)} · IGST {money(row.igst)}{row.cess ? ` · Cess ${money(row.cess)}` : ""}{row.original_invoice_number ? ` · Original invoice ${row.original_invoice_number}` : ""}{row.invoice_status!=="ACTIVE" ? ` · Source status ${row.invoice_status}` : ""}</p>{row.description && <p className="mt-1 text-xs text-slate-500">{row.description}</p>}</details>{row.note_relationship && <div className="mt-3 rounded-lg border border-indigo-200 bg-indigo-50 p-3 text-sm"><p className="font-semibold text-indigo-950">{row.document_type.startsWith("CREDIT_NOTE") ? "Credit note" : "Debit note"} relationship</p>{row.note_relationship.original ? <><p className="mt-1 text-indigo-900">{row.note_relationship.status === "LINKED" ? "Linked to original invoice" : "Original invoice needs confirmation"}: <strong>{row.note_relationship.original.invoice_number}</strong> · {row.note_relationship.original.invoice_date} · {row.note_relationship.original.source}</p><div className="mt-2 grid gap-2 text-xs text-indigo-950 sm:grid-cols-4"><p>Original taxable value<br/><strong>{money(row.note_relationship.original.taxable_value)}</strong></p><p>Original tax<br/><strong>{money(row.note_relationship.original.tax)}</strong></p><p>{row.document_type.startsWith("CREDIT_NOTE") ? "Credit adjustment" : "Debit adjustment"}<br/><strong>{row.document_type.startsWith("CREDIT_NOTE") ? "−" : "+"}{money(row.taxable_value)} taxable · {money(row.cgst+row.sgst+row.igst+row.cess)} tax</strong></p><p>Taxable remaining after credits<br/><strong>{money(row.note_relationship.original.remaining_taxable_value ?? row.note_relationship.original.taxable_value)}</strong></p></div></> : <p className="mt-1 text-indigo-900">{row.note_relationship.status.replaceAll("_"," ").toLowerCase()}. Check the customer, note reference and original invoice date.</p>}<p className="mt-2 border-t border-indigo-200 pt-2 text-xs text-indigo-900">Filed-return check: this app cannot read GST Portal filing history. Check the filed GSTR-1 before reporting this note again.</p></div>}</div><div className="text-right"><p className="font-semibold text-slate-900">{money(row.invoice_value)}</p><p className="text-xs text-slate-500">Invoice total</p></div></div><div className="mt-3">{row.issues.length ? <ul className="space-y-2">{row.issues.map(issue=><li key={issue.id} className={`rounded-lg border px-3 py-3 ${issue.status==="OPEN"?"border-amber-200 bg-amber-50":"border-slate-200 bg-slate-50"}`}><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-sm font-semibold text-slate-900">{issueTitle(issue.code)}</span><span className={`text-[11px] font-semibold ${issue.status==="OPEN"?"text-amber-800":"text-slate-500"}`}>{issue.status==="OPEN"?"Check needed":issue.review_decision?.replaceAll("_"," ") || "Reviewed"}</span></div><p className="mt-2 text-xs font-semibold uppercase tracking-wide text-slate-500">What we found</p><p className="mt-0.5 text-sm leading-5 text-slate-700">{issue.message}</p>{issue.status==="OPEN" && <><p className="mt-2 text-xs font-semibold uppercase tracking-wide text-slate-500">What to check</p><p className="mt-0.5 text-sm leading-5 text-slate-700">{issueNextStep(issue.code)}</p></>}{issue.code==="TAX_RATE_ARITHMETIC" && row.tax_check && <div className="mt-2 grid gap-2 rounded-md border border-rose-200 bg-white p-3 text-xs sm:grid-cols-3"><div><p className="text-slate-500">Expected at entered rate ({row.tax_check.rate}%)</p><p className="mt-0.5 font-semibold text-slate-900">{money(row.tax_check.expected_gst)}</p></div><div><p className="text-slate-500">GST in register</p><p className="mt-0.5 font-semibold text-slate-900">{money(row.tax_check.reported_gst)}</p></div><div><p className="text-slate-500">Difference</p><p className="mt-0.5 font-semibold text-rose-800">{money(row.tax_check.difference)}</p></div></div>}{issue.review_note && <p className="mt-1 text-xs text-slate-600">Review note: {issue.review_note}</p>}{issue.status==="OPEN" && canManageReview && data.status==="DRAFT" && <><textarea value={issueNotes[issue.id] || ""} onChange={(event)=>setIssueNotes((current)=>({...current,[issue.id]:event.target.value}))} placeholder="Optional note to explain your decision" className="mt-2 block w-full rounded border border-amber-200 bg-white px-2 py-1.5 text-xs" rows={2}/><div className="mt-2 flex flex-wrap gap-2"><button disabled={busy} onClick={()=>void decideIssue(issue.id,"ACCEPT_VALID")} className="rounded border border-emerald-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-emerald-800">{issue.code === "NOTE_PORTAL_REPORTING_CHECK" ? "Confirmed not previously reported" : "Confirmed valid"}</button>{issue.code !== "NOTE_PORTAL_REPORTING_CHECK" && <button disabled={busy} onClick={()=>void decideIssue(issue.id,"IGNORE")} className="rounded border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold">Dismiss warning</button>}<button disabled={busy} onClick={()=>void decideIssue(issue.id,"NEEDS_CORRECTION")} className="rounded border border-amber-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-amber-900">Needs correction</button></div></>}</li>)}</ul> : <p className="text-sm text-emerald-700">No checks were raised for this invoice.</p>}</div>{row.status!=="REVIEWED" && !row.issues.some((issue)=>issue.status==="OPEN") && (canManageReview ? <button disabled={busy || data.status!=="DRAFT"} onClick={()=>void review(row.id,"reviewed")} className="mt-3 rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50">Mark invoice reviewed</button> : <p className="mt-3 text-xs text-slate-500">Manager review required.</p>)}</article>)}</div> : <div className="p-8 text-center"><p className="font-medium text-slate-800">No invoices to show</p><p className="mt-1 text-sm text-slate-500">Change the filter or search term to see more invoices.</p></div>}
      </section>
      {data.historical_trend && <section className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><h3 className="font-semibold">Period trend</h3><p className="mt-1 text-sm text-slate-500">{data.historical_trend.basis}</p>{data.historical_trend.anomaly_hints.length>0 && <ul className="mt-3 space-y-1 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">{data.historical_trend.anomaly_hints.map((hint,index)=><li key={index}>Review suggested: {hint}</li>)}</ul>}<div className="mt-3 flex flex-wrap gap-2">{data.historical_trend.periods.map((item)=><span key={item.period} className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-700">{item.period} · {money(item.taxable_value)} · {item.invoice_count} invoices · B2B {(item.b2b_share*100).toFixed(0)}%</span>)}</div></section>}
      <section className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><div><h3 className="font-semibold">GST rate reference</h3><p className="mt-1 text-sm text-slate-600">{data.gst_rate_reference.detail}</p></div>{data.gst_rate_reference.firm_references.length>0 && <ul className="mt-3 space-y-2">{data.gst_rate_reference.firm_references.map((item)=><li key={item.version} className="rounded-lg bg-slate-50 p-3 text-sm"><strong>{item.version}</strong> · effective {item.effective_from}{item.effective_to?` to ${item.effective_to}`:" onward"} · {item.hsn_count} HSN/SAC entries · <a href={item.source_url} target="_blank" rel="noreferrer" className="text-teal-800 underline">Reference source</a></li>)}</ul>}{canManageRateReference ? <div className="mt-4 border-t border-slate-100 pt-4"><h4 className="text-sm font-semibold">Add a firm-maintained version</h4><p className="mt-1 text-xs text-slate-500">Use a reviewed source and the dates when it applies. A rate difference is always a review prompt, not a legal conclusion. New versions are retained in the audit history.</p><div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><label className="text-xs font-medium text-slate-600">Version<input value={rateVersion} onChange={e=>setRateVersion(e.target.value)} placeholder="e.g. rates-2026-10-v1" className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"/></label><label className="text-xs font-medium text-slate-600">Effective from<input type="date" value={rateEffectiveFrom} onChange={e=>setRateEffectiveFrom(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"/></label><label className="text-xs font-medium text-slate-600">Effective to (optional)<input type="date" value={rateEffectiveTo} onChange={e=>setRateEffectiveTo(e.target.value)} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"/></label><label className="text-xs font-medium text-slate-600 sm:col-span-2 lg:col-span-1">Source URL<input type="url" value={rateSourceUrl} onChange={e=>setRateSourceUrl(e.target.value)} placeholder="https://..." className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"/></label></div><label className="mt-3 block text-xs font-medium text-slate-600">Rates by HSN/SAC (JSON)<textarea value={rateMapText} onChange={e=>setRateMapText(e.target.value)} rows={5} className="mt-1 block w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-sm"/><span className="mt-1 block font-normal text-slate-500">Example: {`{"9983":["12","18"],"1001":"5"}`}. Multiple rate options are allowed when the treatment depends on facts.</span></label><button disabled={busy || !rateVersion.trim() || !rateEffectiveFrom || !rateSourceUrl.trim()} onClick={()=>void saveRateReference()} className="mt-3 rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Save reference version</button></div> : <p className="mt-3 text-xs text-slate-500">Only the firm owner can add or replace firm-maintained rate references.</p>}</section>
      <section className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="font-semibold">Client follow-up</h3><p className="mt-1 text-sm text-slate-500">Draft a grouped request from the open checks. Review it before sending through your usual channel.</p></div><button onClick={prepareClientQuery} className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-semibold">Create query draft</button></div>{clientQuery && <><textarea readOnly value={clientQuery} rows={Math.min(12, Math.max(4, clientQuery.split("\n").length))} className="mt-3 block w-full rounded-lg border border-slate-300 p-3 text-sm"/><button onClick={()=>void copyClientQuery()} className="mt-2 rounded-lg bg-slate-900 px-3 py-2 text-sm font-semibold text-white">Copy query</button></>}</section>
      <p className="mt-4 text-xs leading-5 text-slate-500">The generated JSON is a review draft and is not a GST Portal filing payload. Rate comparisons use effective-dated references supplied by the firm or platform; differences remain review prompts because exemptions, notifications and supply facts can change treatment.</p>
      <section className="mt-5 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-5">
          <div>
            <p className="text-xs font-semibold uppercase tracking-wide text-teal-700">{data.annexure_report.period_label}</p>
            <h3 className="mt-1 text-xl font-semibold text-slate-900">Annexure sales summary</h3>
            <p className="mt-1 text-sm text-slate-500">A period-wide summary grouped by GSTR-1 reporting category. Invoice-level checks are in the review queue above.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button onClick={()=>void downloadAnnexure()} className="rounded-lg bg-teal-700 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-teal-800">Download summary Excel</button>
            <button onClick={()=>void downloadAnnexurePdf()} className="rounded-lg border border-slate-300 px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50">Printable summary</button>
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <span>Separate report</span>
              <select value={annexureReportType} onChange={(event)=>setAnnexureReportType(event.target.value as typeof annexureReportType)} className="rounded-lg border border-slate-300 bg-white px-3 py-2.5">
                <option value="b2b">B2B Annexure</option>
                <option value="b2c">B2C Annexure</option>
                <option value="hsn_b2b">HSN/SAC - B2B</option>
                <option value="hsn_summary">HSN/SAC Summary - All</option>
                <option value="exports">Exports / SEZ</option>
                <option value="notes">Credit / Debit Notes</option>
                <option value="sales">All sales transactions</option>
              </select>
            </label>
            <button onClick={()=>void downloadAnnexureReport()} className="rounded-lg border border-teal-700 px-4 py-2.5 text-sm font-semibold text-teal-800 hover:bg-teal-50">Download selected Excel</button>
          </div>
        </div>
        <div className="grid gap-3 border-b border-slate-100 bg-slate-50/70 p-4 sm:grid-cols-2 lg:grid-cols-4">
          <Metric title="Total documents" value={data.annexure_report.totals.records} />
          <Metric title="Net taxable sales" value={annexureMoney(Number(data.annexure_report.totals.taxable_amount))} hint="Credit notes reduce this total." />
          <Metric title="Total GST" value={annexureMoney(Number(data.annexure_report.totals.cgst + data.annexure_report.totals.sgst + data.annexure_report.totals.igst + data.annexure_report.totals.cess))} hint={`CGST ${annexureMoney(Number(data.annexure_report.totals.cgst))} · SGST ${annexureMoney(Number(data.annexure_report.totals.sgst))} · IGST ${annexureMoney(Number(data.annexure_report.totals.igst))}`} />
          <Metric title="Need category review" value={data.annexure_report.rows.find((row)=>row.particulars==="Needs category review")?.records || 0} hint="Records without a clear reporting group." />
        </div>
        <div className="overflow-x-auto px-5 py-4">
          <table className="min-w-[850px] w-full text-left text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-600">
              <tr><th className="px-3 py-3">Sales category</th><th className="px-3 py-3 text-right">Documents</th><th className="px-3 py-3 text-right">Taxable amount</th><th className="px-3 py-3 text-right">CGST</th><th className="px-3 py-3 text-right">SGST</th><th className="px-3 py-3 text-right">IGST</th><th className="px-3 py-3 text-right">Cess</th><th className="px-3 py-3 text-right">Export / SEZ included</th><th className="px-3 py-3 text-right">Export IGST included</th></tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.annexure_report.rows.filter((row)=>row.records > 0).map((row)=><tr key={row.particulars} className={row.particulars==="Needs category review"?"bg-amber-50":"hover:bg-slate-50"}>
                <td className="px-3 py-3 font-medium text-slate-900">{row.particulars}</td><td className="px-3 py-3 text-right">{row.records.toLocaleString("en-IN")}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(row.taxable_amount))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(row.cgst))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(row.sgst))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(row.igst))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(row.cess))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(row.export_sez))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(row.igst_export))}</td>
              </tr>)}
              <tr className="border-t-2 border-slate-300 bg-slate-100 font-bold text-slate-950"><td className="px-3 py-3">Total</td><td className="px-3 py-3 text-right">{data.annexure_report.totals.records.toLocaleString("en-IN")}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(data.annexure_report.totals.taxable_amount))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(data.annexure_report.totals.cgst))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(data.annexure_report.totals.sgst))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(data.annexure_report.totals.igst))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(data.annexure_report.totals.cess))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(data.annexure_report.totals.export_sez))}</td><td className="px-3 py-3 text-right">{annexureMoney(Number(data.annexure_report.totals.igst_export))}</td></tr>
            </tbody>
          </table>
        </div>
        <p className="border-t border-slate-100 px-5 py-3 text-xs leading-5 text-slate-500">{data.annexure_report.note} These export/SEZ columns are included in the taxable and GST totals; do not add them again. Figures use the imported sales register and are not a GST Portal reconciliation.</p>
      </section>
    </>}
  </main>;
}

function Metric({title,value,warn=false,onClick,hint}:{title:string;value:number|string;warn?:boolean;onClick?:()=>void;hint?:string}) {
  const content = <><p className="text-sm text-slate-500">{title}</p><p className={`mt-1 text-2xl font-semibold ${warn?"text-amber-700":"text-slate-900"}`}>{typeof value === "number" ? value.toLocaleString("en-IN") : value}</p>{hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}</>;
  const styles = `w-full rounded-xl border border-slate-200 bg-white p-4 text-left ${warn?"border-amber-200":""}`;
  return onClick ? <button type="button" onClick={onClick} className={`${styles} cursor-pointer transition hover:border-teal-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-teal-600`}>{content}</button> : <div className={styles}>{content}</div>;
}

function moneyCompact(amount:number) { return new Intl.NumberFormat("en-IN", {style:"currency",currency:"INR",notation:"compact",maximumFractionDigits:2}).format(amount); }
function periodLabel(period:string) { const [year,month] = period.split("-").map(Number); return new Intl.DateTimeFormat("en-IN",{month:"long",year:"numeric"}).format(new Date(year,month-1,1)); }

function issueTitle(code: string) {
  const titles: Record<string, string> = {
    GSTIN_INVALID: "Recipient GSTIN needs checking", RECIPIENT_GSTIN_POS_MISMATCH: "GSTIN state and place of supply differ", PLACE_OF_SUPPLY_UNRECOGNIZED: "Place of supply needs checking", GST_RATE_REFERENCE_MISMATCH: "GST rate differs from configured reference · review required", GST_RATE_REFERENCE_NOT_FOUND: "No configured rate for this HSN/SAC", GST_RATE_MISSING: "Invoice GST rate is missing", INVOICE_NUMBER_REUSED: "Invoice number reused with different details", POSSIBLE_DUPLICATE: "Possible duplicate invoice",
    POSSIBLE_FUZZY_DUPLICATE: "Invoice number resembles another invoice", POSSIBLE_DUPLICATE_NOTE: "Possible duplicate credit/debit note", TOTAL_MISMATCH: "Invoice total does not add up",
    TAX_SPLIT_CONFLICT: "IGST and CGST/SGST both entered", PLACE_OF_SUPPLY_TAX_SPLIT: "Tax split needs review for seller and place-of-supply states",
    TAX_RATE_ARITHMETIC: "Tax amount mismatch using entered rate", PERIOD_MISMATCH: "Invoice date is outside this period",
    HSN_MISSING: "HSN/SAC is missing", INVOICE_SEQUENCE_GAP: "Gap in invoice number sequence",
    HSN_FORMAT_REVIEW: "HSN/SAC format needs checking", CANCELLED_INVOICE_INCLUDED: "Cancelled invoice included in register",
    NOTE_ORIGINAL_MISSING: "Original invoice number is missing", NOTE_ORIGINAL_NOT_FOUND: "Original invoice could not be matched",
    NOTE_EXCEEDS_ORIGINAL: "Note value is higher than the original invoice",
    NOTE_ORIGINAL_AMBIGUOUS: "More than one original invoice matches", NOTE_PREVIOUSLY_PREPARED: "Note appeared in an earlier register",
    NOTE_PORTAL_REPORTING_CHECK: "Check note against filed returns",
    CUSTOMER_GSTIN_INCONSISTENT: "Customer has different GSTINs in this register",
    CUSTOMER_GSTIN_DIFFERS_FROM_HISTORY: "Customer GSTIN differs from earlier periods",
    CUSTOMER_HSN_CHANGED_FROM_HISTORY: "HSN/SAC differs from earlier periods",
    INVOICE_STATUS_UNRECOGNIZED: "Invoice status needs checking",
  };
  return titles[code] || code.replaceAll("_", " ").toLowerCase().replace(/^./, (letter) => letter.toUpperCase());
}

function issueNextStep(code: string) {
  const steps: Record<string, string> = {
    DOCUMENT_TYPE_REQUIRES_REVIEW: "Check the source document. Select Invoice, Credit Note or Debit Note as appropriate. B2B/B2C describes the customer category, not the document type.",
    TOTAL_MISMATCH: "Compare the invoice total with taxable value plus taxes and any charges shown on the original invoice. If this file has one row per product, check whether the full invoice total was repeated on every row.",
    INVOICE_NUMBER_REUSED: "Open both rows with this invoice number. They may be duplicate invoices, or valid product lines from the same invoice. Confirm using the original invoice before changing the register.",
    POSSIBLE_DUPLICATE: "Compare the customer, invoice date, taxable amount and tax on both records. Keep both if they are separate valid transactions.",
    POSSIBLE_FUZZY_DUPLICATE: "Compare the similar invoice numbers and amounts with the source documents. A similar number alone does not prove a duplicate.",
    TAX_RATE_ARITHMETIC: "Check the taxable amount, entered GST rate and tax amounts against the invoice. The expected amount is a calculation prompt, not a tax-rate ruling.",
    TAX_SPLIT_CONFLICT: "Check whether the invoice should show IGST or CGST plus SGST, then compare the tax columns with the original invoice.",
    PLACE_OF_SUPPLY_TAX_SPLIT: "Confirm the seller state and place of supply, then check the IGST or CGST/SGST split against the invoice.",
    GSTIN_INVALID: "Compare the recipient GSTIN with the customer master or original invoice. Correct it in the source register if it is mistyped.",
    RECIPIENT_GSTIN_POS_MISMATCH: "Check the recipient GSTIN state code and place of supply against the invoice. A difference may need an explanation.",
    PLACE_OF_SUPPLY_UNRECOGNIZED: "Check the place of supply in the source register and confirm it matches the invoice.",
    HSN_MISSING: "Check the invoice for its HSN/SAC and add it to the source register if available and required.",
    HSN_FORMAT_REVIEW: "Compare the HSN/SAC in the register with the invoice. This format check does not confirm the product classification.",
    GST_RATE_REFERENCE_MISMATCH: "Compare the entered rate with the configured reference and the invoice. Ask a tax reviewer to confirm the treatment for this supply.",
    GST_RATE_REFERENCE_NOT_FOUND: "Check whether a rate reference exists for this HSN/SAC and period. The missing reference is not proof that the invoice rate is wrong.",
    GST_RATE_MISSING: "Check the invoice for the applicable rate and update the source register if it is missing.",
    PERIOD_MISMATCH: "Check the invoice date and confirm this document belongs in the selected return period.",
    CANCELLED_INVOICE_INCLUDED: "Check the invoice status in the books. Remove or correct it in a new source upload if it was cancelled.",
    NOTE_ORIGINAL_MISSING: "Check the credit/debit note for the original invoice reference and add it to the source register if available.",
    NOTE_ORIGINAL_NOT_FOUND: "Search the register for the original invoice. Confirm the reference and period with the source document.",
    NOTE_ORIGINAL_AMBIGUOUS: "Compare the candidate invoices with the note. Add the original invoice date in the source file so the system can identify the right one.",
    NOTE_EXCEEDS_ORIGINAL: "Compare all credit notes for this original invoice with its taxable value. Check prior adjustments and the remaining balance.",
    NOTE_PREVIOUSLY_PREPARED: "Open the earlier-period preparation and check the filed GSTR-1 or acknowledgement. An earlier preparation does not prove the note was filed.",
    NOTE_PORTAL_REPORTING_CHECK: "Search the filed GSTR-1 or filing acknowledgement for this note number and period. Choose the confirmation only after verifying that it has not already been reported; if it was reported, keep this item open and correct the register.",
    POSSIBLE_DUPLICATE_NOTE: "Compare the customer, note number, note date, taxable value and tax. Repeated product lines can be legitimate; keep both unless the source documents confirm a duplicate note.",
    INVOICE_SEQUENCE_GAP: "Check whether the missing number was cancelled or unused. A sequence gap alone is not an error.",
  };
  return steps[code] || "Compare this detail with the original invoice or customer records. If the source is correct, confirm it; otherwise correct the source register and upload it again.";
}

function classificationLabel(value:string) { return reportingCategoryLabel(value); }
function documentTypeLabel(value:string) {
  const labels:Record<string,string> = {INVOICE:"Invoice", CREDIT_NOTE:"Credit note", DEBIT_NOTE:"Debit note", EXPORT:"Export document", SEZ:"SEZ document", DEEMED_EXPORT:"Deemed export document", OTHER:"Source type needs review"};
  return labels[value] || value.replaceAll("_", " ");
}
function reportingCategoryLabel(value:string) {
  const labels:Record<string,string> = {B2B:"B2B", B2CL:"B2C large (B2CL)", B2CS:"B2C other (B2CS)", EXPORT:"Export", SEZ:"SEZ supply", DEEMED_EXPORT:"Deemed export", CREDIT_NOTE:"Credit note", DEBIT_NOTE:"Debit note", UNCLASSIFIED:"Needs classification", OTHER:"Needs classification review"};
  return labels[value] || value.replaceAll("_", " ");
}
