import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";
import type {
  AppNotification,
  AuditEntry,
  AuthSession,
  Client,
  ClientGSTIN,
  DashboardSummary,
  ConsentVerifyResult,
  ExceptionListItem,
  FetchAttempt,
  FetchResult,
  GspFetchResult,
  GspStatus,
  GstinInfo,
  NoticeDetail,
  NoticeEvidence,
  NoticeHistoryEntry,
  NoticeStats,
  NoticeSummary,
  NoticeTypeOption,
  NoticeUpdate,
  AuditBrief,
  AuditEngagement,
  ManagementRepLetter,
  AuditWorkingPaper,
  AuditDashboardItem,
  AuditProcedure,
  RelatedParty,
  CAROClauseResponse,
  CAROApplicabilityAssessment,
  TrialBalanceUpload,
  AuditRiskFlag,
  AuditAgeingSchedule,
  StatutoryDuesRow,
  TrialBalanceLine,
  ClientBankAccount,
  BankConfirmation,
  AuditRatioAnalysis,
  NotificationPreference,
  Portfolio,
  OtpRequestResult,
  PeriodInfo,
  ReconciliationResult,
  UploadRecord,
  UploadResult,
  User,
} from "../types";

export const API_BASE =
  (import.meta.env.VITE_API_BASE as string | undefined) ?? "http://localhost:8000/api";

/**
 * The access token is held in memory only, never in localStorage.
 *
 * A token in localStorage is readable by any script on the page, so a single
 * cross-site scripting bug hands an attacker a valid session. Keeping it in a
 * module variable means it dies with the tab; the httpOnly refresh cookie
 * restores the session on reload, and the browser will not let JavaScript
 * read that cookie at all.
 */
let accessToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function getAccessToken() {
  return accessToken;
}

export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler;
}

// `withCredentials` is required for the refresh cookie to be sent at all.
const http = axios.create({ baseURL: API_BASE, withCredentials: true });

http.interceptors.request.use((config) => {
  if (accessToken) {
    config.headers.Authorization = `Bearer ${accessToken}`;
  }
  return config;
});

/**
 * Refresh-on-401, with a single in-flight refresh shared by all callers.
 *
 * Access tokens last 15 minutes, so expiry mid-session is normal rather than
 * exceptional. Without the shared promise, a dashboard firing six parallel
 * requests would trigger six refreshes, and because refresh tokens rotate,
 * five of them would present an already-spent token — which the server
 * correctly treats as theft and responds to by ending every session.
 */
let refreshPromise: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  if (!refreshPromise) {
    refreshPromise = axios
      .post<AuthSession>(`${API_BASE}/auth/refresh`, null, { withCredentials: true })
      .then((res) => {
        accessToken = res.data.access_token;
        return res.data.access_token;
      })
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

interface RetriableRequest extends InternalAxiosRequestConfig {
  _retried?: boolean;
}

http.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as RetriableRequest | undefined;
    const status = error.response?.status;
    const url = original?.url ?? "";

    // Never try to refresh a failed refresh, or a failed login.
    const isAuthEndpoint =
      url.includes("/auth/refresh") ||
      url.includes("/auth/login") ||
      url.includes("/auth/signup");

    if (status === 401 && original && !original._retried && !isAuthEndpoint) {
      original._retried = true;
      try {
        const token = await refreshAccessToken();
        original.headers.Authorization = `Bearer ${token}`;
        return http(original);
      } catch {
        accessToken = null;
        onUnauthorized?.();
      }
    }
    return Promise.reject(error);
  },
);

export function getErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail;
    if (typeof detail === "string") return detail;
    // FastAPI validation errors arrive as a list of objects.
    if (Array.isArray(detail)) {
      return detail
        .map((d: { loc?: string[]; msg?: string }) => {
          const field = d.loc?.filter((p) => p !== "body").join(".");
          return field ? `${field}: ${d.msg}` : d.msg;
        })
        .join("; ");
    }
    if (err.response?.status === 429) {
      return "Too many requests. Wait a moment and try again.";
    }
    return err.message;
  }
  return String(err);
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export interface SignupPayload {
  firm_name: string;
  full_name: string;
  email: string;
  password: string;
  phone?: string;
  firm_gstin?: string;
  firm_pan?: string;
}

export async function signup(payload: SignupPayload): Promise<AuthSession> {
  const { data } = await http.post<AuthSession>("/auth/signup", payload);
  accessToken = data.access_token;
  return data;
}

export async function login(
  email: string,
  password: string,
  totpCode?: string,
): Promise<AuthSession> {
  const { data } = await http.post<AuthSession>("/auth/login", {
    email,
    password,
    totp_code: totpCode || undefined,
  });
  accessToken = data.access_token;
  return data;
}

export async function restoreSession(): Promise<AuthSession> {
  const { data } = await axios.post<AuthSession>(`${API_BASE}/auth/refresh`, null, {
    withCredentials: true,
  });
  accessToken = data.access_token;
  return data;
}

export async function logout(): Promise<void> {
  try {
    await http.post("/auth/logout");
  } finally {
    accessToken = null;
  }
}

export async function logoutAllDevices(): Promise<{ message: string }> {
  const { data } = await http.post("/auth/logout-all");
  accessToken = null;
  return data;
}

export async function listFirmUsers(): Promise<User[]> {
  const { data } = await http.get<User[]>("/auth/users");
  return data;
}

export async function inviteUser(
  email: string,
  role: string,
  scopedClientId?: string,
): Promise<unknown> {
  const { data } = await http.post("/auth/invitations", {
    email,
    role,
    scoped_client_id: scopedClientId,
  });
  return data;
}

export async function getAuditLog(limit = 100): Promise<AuditEntry[]> {
  const { data } = await http.get<AuditEntry[]>("/auth/audit-log", { params: { limit } });
  return data;
}

export async function verifyAuditLog(): Promise<{
  valid: boolean;
  checked: number;
  broken_at: string | null;
  reason: string | null;
}> {
  const { data } = await http.get("/auth/audit-log/verify");
  return data;
}

// ---------------------------------------------------------------------------
// Clients
// ---------------------------------------------------------------------------

export async function listClients(): Promise<Client[]> {
  const { data } = await http.get<Client[]>("/clients");
  return data;
}

export async function getClient(clientId: string): Promise<Client> {
  const { data } = await http.get<Client>(`/clients/${clientId}`);
  return data;
}

export async function uploadGstr1SalesRegister(gstinId: string, period: string, file: File,
  dateMeaning?: string, dateColumn?: string, columnChoices?: Record<string,string>,
  businessColumnChoices?: Record<string,string>): Promise<{uploadId:string;returnId:string;period:string;version:number;status:string;recordsProcessed:number;recordsAccepted:number;recordsRequiringReview:number;criticalIssueCount:number;message:string}> {
  const form = new FormData();
  form.append("file", file);
  if (dateMeaning && dateColumn) {
    form.append("date_interpretation", dateMeaning);
    form.append("date_column", dateColumn);
  }
  if (columnChoices && Object.keys(columnChoices).length) form.append("column_choices", JSON.stringify(columnChoices));
  if (businessColumnChoices && Object.keys(businessColumnChoices).length) form.append("business_column_choices", JSON.stringify(businessColumnChoices));
  const { data } = await http.post(`/gstr1/gstins/${gstinId}/uploads`, form, { params: { period } });
  return data;
}

export async function reconcileGstr1Books(returnId: string, file: File): Promise<any> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await http.post(`/gstr1/returns/${returnId}/reconcile-books`, form);
  return data;
}

export async function getGstr1Return(gstinId: string, period: string): Promise<any> {
  const { data } = await http.get(`/gstr1/gstins/${gstinId}/returns/${period}`);
  const fromBusinessCase = (value: any): any => Array.isArray(value) ? value.map(fromBusinessCase)
    : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).map(([key,item]) =>
      [key.replace(/[A-Z]/g, letter => `_${letter.toLowerCase()}`), fromBusinessCase(item)])) : value;
  const {invoices, ...workspace} = data;
  // Translate public business vocabulary into the existing page view model.
  return {...fromBusinessCase(workspace), invoices: invoices.map((row: any) => ({
    id: row.id, row_number: row.sourceRowNumber, invoice_number: row.invoiceNumber,
    invoice_date: row.invoiceDate, customer_name: row.customer, customer_gstin: row.customerGstin,
    invoice_value: row.invoiceValue, taxable_value: row.taxableValue, cgst: row.cgstAmount,
    sgst: row.sgstAmount, igst: row.igstAmount, cess: row.cessAmount, gst_rate: row.gstRate,
    tax_check: row.taxCheck ? {rate:row.taxCheck.enteredRate,expected_gst:row.taxCheck.expectedGst,
      reported_gst:row.taxCheck.reportedGst,difference:row.taxCheck.difference} : null,
    hsn_sac: row.hsnSac, description: row.description,
    original_invoice_number: row.originalInvoiceNumber, invoice_status: row.invoiceStatus,
    place_of_supply: row.placeOfSupply, document_type: row.documentType, supply_type: row.supplyType,
    classification: row.category,
    classification_reason: row.categoryReason, status: row.reviewStatus, issues: fromBusinessCase(row.issues),
  }))};
}

export async function getGstr1UploadVersions(gstinId: string, period: string): Promise<Array<{id:string; period:string; version:number; is_current:boolean; original_filename:string; row_count:number; issue_count:number; uploaded_at:string}>> {
  const { data } = await http.get(`/gstr1/gstins/${gstinId}/uploads`, {params:{period}});
  return data;
}

export async function downloadGstr1UploadSource(uploadId: string): Promise<Blob> {
  const { data } = await http.get(`/gstr1/uploads/${uploadId}/source`, { responseType: "blob" });
  return data;
}

export async function getGstr1Checklist(returnId: string): Promise<any> {
  const { data } = await http.get(`/gstr1/returns/${returnId}/checklist`);
  return data;
}

export async function saveGstr1RateReference(reference: {version:string; effective_from:string; effective_to?:string; source_url:string; rates_by_hsn:Record<string,string|string[]>}): Promise<any> {
  const { data } = await http.post("/gstr1/rate-references", reference);
  return data;
}

export async function exportDraftGstr1(gstinId: string, period: string): Promise<any> {
  const { data } = await http.get(`/gstr1/gstins/${gstinId}/returns/${period}/draft-json`);
  return data;
}

export async function exportDraftGstr1Workbook(gstinId: string, period: string): Promise<Blob> {
  const { data } = await http.get(`/gstr1/gstins/${gstinId}/returns/${period}/draft.xlsx`, {responseType:"blob"});
  return data;
}

export async function exportGstr1AnnexureWorkbook(gstinId: string, period: string): Promise<Blob> {
  const { data } = await http.get(`/gstr1/gstins/${gstinId}/returns/${period}/annexure.xlsx`, {responseType:"blob"});
  return data;
}

export async function exportGstr1AnnexureReport(gstinId: string, period: string,
    reportType: "sales"|"b2b"|"b2c"|"exports"|"notes"|"hsn_b2b"|"hsn_summary"): Promise<Blob> {
  const path = "/gstr1/gstins/" + gstinId + "/returns/" + period + "/annexure-report.xlsx";
  const { data } = await http.get(path, {params:{report_type:reportType},responseType:"blob"});
  return data;
}

export async function exportGstr1AnnexurePdf(gstinId: string, period: string): Promise<Blob> {
  const { data } = await http.get(`/gstr1/gstins/${gstinId}/returns/${period}/annexure.pdf`, {responseType:"blob"});
  return data;
}

export async function reviewGstr1Invoice(invoiceId: string, decision: "reviewed" | "needs_review", note?: string): Promise<any> {
  const { data } = await http.post(`/gstr1/invoices/${invoiceId}/review`, { decision, note });
  return data;
}

export async function decideGstr1Issue(issueId: string, decision: "ACCEPT_VALID" | "IGNORE" | "NEEDS_CORRECTION" | "REOPEN", note?: string): Promise<any> {
  const { data } = await http.post(`/gstr1/issues/${issueId}/decision`, { decision, note });
  return data;
}

export async function submitGstr1ForPartnerReview(returnId: string): Promise<any> {
  const { data } = await http.post(`/gstr1/returns/${returnId}/submit-review`);
  return data;
}

export async function approveGstr1Return(returnId: string): Promise<any> {
  const { data } = await http.post(`/gstr1/returns/${returnId}/approve`);
  return data;
}

export async function exportApprovedGstr1(returnId: string): Promise<any> {
  const { data } = await http.get(`/gstr1/returns/${returnId}/approved-export`);
  return data;
}

export async function recordGstr1DocumentsIssued(returnId: string, payload: {document_type:string; series_from:string; series_to:string; issued_count:number; cancelled_count:number}): Promise<any> {
  const { data } = await http.post(`/gstr1/returns/${returnId}/documents-issued`, payload);
  return data;
}

export async function reviewGstr1DocumentsIssued(recordId: string): Promise<any> {
  const { data } = await http.post(`/gstr1/documents-issued/${recordId}/review`);
  return data;
}

// ---------------------------------------------------------------------------
// Audit engagements, planning, related parties, and CARO workflow
// ---------------------------------------------------------------------------

export async function listAuditEngagements(params: { client_id?: string; financial_year?: string } = {}): Promise<AuditEngagement[]> {
  const { data } = await http.get<AuditEngagement[]>("/audit-engagements", { params });
  return data;
}

export async function createAuditEngagement(payload: {
    client_id: string; financial_year: string; engagement_type: "STATUTORY" | "TAX" | "INTERNAL";
    engagement_partner_id: string; reporting_deadline?: string;
}): Promise<AuditEngagement> {
  const { data } = await http.post<AuditEngagement>("/audit-engagements", payload);
  return data;
}

export async function getAuditDashboard(): Promise<{active_engagements: AuditDashboardItem[]; workload_by_staff: Array<{staff_id: string; staff_name?: string; role?: string; engagement_count: number}>; active_count: number; due_soon_count: number}> {
  const { data } = await http.get("/audit-engagements/dashboard/summary"); return data;
}
export async function listManagementRepLetters(id: string): Promise<ManagementRepLetter[]> {
  const { data } = await http.get<ManagementRepLetter[]>(`/audit-engagements/${id}/management-rep-letters`); return data;
}
export async function generateManagementRepLetter(id: string): Promise<ManagementRepLetter> {
  const { data } = await http.post<ManagementRepLetter>(`/audit-engagements/${id}/management-rep-letters/generate`); return data;
}
export async function editManagementRepLetter(id: string, letterId: string, final_content: string): Promise<ManagementRepLetter> {
  const { data } = await http.put<ManagementRepLetter>(`/audit-engagements/${id}/management-rep-letters/${letterId}/edit`, {final_content}); return data;
}
export async function reviewManagementRepLetter(id: string, letterId: string, decision: "approve" | "rework"): Promise<ManagementRepLetter> {
  const { data } = await http.post<ManagementRepLetter>(`/audit-engagements/${id}/management-rep-letters/${letterId}/review`, {decision}); return data;
}
export async function approveManagementRepLetter(id: string, letterId: string): Promise<ManagementRepLetter> {
  const { data } = await http.post<ManagementRepLetter>(`/audit-engagements/${id}/management-rep-letters/${letterId}/approve`); return data;
}
export async function markManagementRepLetter(id: string, letterId: string, action: "send" | "receive"): Promise<ManagementRepLetter> {
  const { data } = await http.post<ManagementRepLetter>(`/audit-engagements/${id}/management-rep-letters/${letterId}/${action}`); return data;
}
export async function listAuditWorkingPapers(id: string): Promise<AuditWorkingPaper[]> {
  const { data } = await http.get<AuditWorkingPaper[]>(`/audit-engagements/${id}/working-papers`); return data;
}
export async function compileAuditWorkingPaper(id: string): Promise<AuditWorkingPaper> {
  const { data } = await http.post<AuditWorkingPaper>(`/audit-engagements/${id}/working-papers/compile`); return data;
}
export async function reviewAuditWorkingPaper(id: string, paperId: string, decision: "approve" | "rework"): Promise<AuditWorkingPaper> {
  const { data } = await http.post<AuditWorkingPaper>(`/audit-engagements/${id}/working-papers/${paperId}/review`, {decision}); return data;
}
export async function approveAuditWorkingPaper(id: string, paperId: string): Promise<AuditWorkingPaper> {
  const { data } = await http.post<AuditWorkingPaper>(`/audit-engagements/${id}/working-papers/${paperId}/approve`); return data;
}
export async function exportAuditPackage(id: string): Promise<Blob> {
  const { data } = await http.get(`/audit-engagements/${id}/audit-package/export`, {responseType: "blob"}); return data;
}
export async function advanceAuditEngagement(id: string, target: "completion" | "reporting" | "close"): Promise<{id: string; status: string}> {
  const path = target === "close" ? "close" : `move-to-${target}`;
  const { data } = await http.post(`/audit-engagements/${id}/${path}`); return data;
}
export async function listAuditProcedures(id: string): Promise<AuditProcedure[]> {
  const { data } = await http.get<AuditProcedure[]>(`/audit-engagements/${id}/procedures`); return data;
}
export async function createAuditProcedure(id: string, payload: {sa_reference: string; procedure_name: string}): Promise<AuditProcedure> {
  const { data } = await http.post<AuditProcedure>(`/audit-engagements/${id}/procedures`, payload); return data;
}
export async function performAuditProcedure(id: string, procedureId: string, payload: {conclusion_notes: string; evidence_file_paths: string[]}): Promise<AuditProcedure> {
  const { data } = await http.post<AuditProcedure>(`/audit-engagements/${id}/procedures/${procedureId}/perform`, payload); return data;
}
export async function reviewAuditProcedure(id: string, procedureId: string, decision: "approve" | "rework"): Promise<AuditProcedure> {
  const { data } = await http.post<AuditProcedure>(`/audit-engagements/${id}/procedures/${procedureId}/review`, {decision}); return data;
}
export async function approveAuditProcedure(id: string, procedureId: string): Promise<AuditProcedure> {
  const { data } = await http.post<AuditProcedure>(`/audit-engagements/${id}/procedures/${procedureId}/approve`); return data;
}

export async function getAuditEngagement(id: string): Promise<AuditEngagement> {
  const { data } = await http.get<AuditEngagement>(`/audit-engagements/${id}`);
  return data;
}

export async function suggestAuditMateriality(id: string, payload: {
  turnover: string; profit_before_tax: string; net_worth: string;
}): Promise<{ id: string; suggested_amount: string; basis: string; percentage: string; status: string }> {
  const { data } = await http.post(`/audit-engagements/${id}/materiality/suggest`, payload);
  return data;
}

export async function confirmAuditMateriality(id: string, payload: {
  confirmed_amount?: string; override_rationale?: string;
}): Promise<unknown> {
  const { data } = await http.post(`/audit-engagements/${id}/materiality/confirm`, payload);
  return data;
}

export async function assessCaroApplicability(id: string, payload: Record<string, boolean | string>): Promise<CAROApplicabilityAssessment> {
  const { data } = await http.post<CAROApplicabilityAssessment>(`/audit-engagements/${id}/caro-applicability/assess`, payload);
  return data;
}

export async function confirmCaroApplicability(id: string, applicable: boolean, rationale?: string): Promise<CAROApplicabilityAssessment> {
  const { data } = await http.post<CAROApplicabilityAssessment>(`/audit-engagements/${id}/caro-applicability/confirm`, { applicable, rationale });
  return data;
}

export async function startAuditFieldwork(id: string): Promise<AuditEngagement> {
  const { data } = await http.post<AuditEngagement>(`/audit-engagements/${id}/start-fieldwork`);
  return data;
}

export async function listRelatedParties(clientId: string): Promise<RelatedParty[]> {
  const { data } = await http.get<RelatedParty[]>(`/clients/${clientId}/related-parties`);
  return data;
}

export async function createRelatedParty(clientId: string, payload: {
  name: string; aliases: string[]; pan?: string; relationship_nature: string;
  effective_from?: string; effective_to?: string;
}): Promise<RelatedParty> {
  const { data } = await http.post<RelatedParty>(`/clients/${clientId}/related-parties`, payload);
  return data;
}

export async function updateRelatedParty(clientId: string, id: string, payload: Partial<{
  name: string; aliases: string[]; pan: string; relationship_nature: string;
  effective_from: string | null; effective_to: string | null; is_active: boolean;
}>): Promise<RelatedParty> {
  const { data } = await http.patch<RelatedParty>(`/clients/${clientId}/related-parties/${id}`, payload);
  return data;
}

export async function deactivateRelatedParty(clientId: string, id: string): Promise<RelatedParty> {
  const { data } = await http.delete<RelatedParty>(`/clients/${clientId}/related-parties/${id}`);
  return data;
}

export async function listCaroResponses(engagementId: string): Promise<CAROClauseResponse[]> {
  const { data } = await http.get<CAROClauseResponse[]>(`/audit-engagements/${engagementId}/caro-responses`);
  return data;
}

export async function generateCaroDrafts(engagementId: string): Promise<CAROClauseResponse[]> {
  const { data } = await http.post<CAROClauseResponse[]>(`/audit-engagements/${engagementId}/caro-responses/generate`);
  return data;
}

export async function editCaroResponse(engagementId: string, responseId: string, finalNarrative: string): Promise<CAROClauseResponse> {
  const { data } = await http.put<CAROClauseResponse>(`/audit-engagements/${engagementId}/caro-responses/${responseId}/edit`, { final_narrative: finalNarrative });
  return data;
}

export async function reviewCaroResponse(engagementId: string, responseId: string,
                                         decision: "approve" | "rework", notes?: string): Promise<CAROClauseResponse> {
  const { data } = await http.post<CAROClauseResponse>(`/audit-engagements/${engagementId}/caro-responses/${responseId}/review`, { decision, notes });
  return data;
}

export async function finalizeCaroResponse(engagementId: string, responseId: string,
                                           finalNarrative: string): Promise<CAROClauseResponse> {
  const { data } = await http.post<CAROClauseResponse>(`/audit-engagements/${engagementId}/caro-responses/${responseId}/finalize`, { final_narrative: finalNarrative });
  return data;
}

export async function listTrialBalanceUploads(engagementId: string): Promise<TrialBalanceUpload[]> {
  const { data } = await http.get<TrialBalanceUpload[]>(`/audit-engagements/${engagementId}/trial-balances`);
  return data;
}

export async function uploadComparativeTrialBalance(engagementId: string, currentFile: File, priorFile: File): Promise<Record<string, unknown>> {
  const body = new FormData(); body.append("current_file", currentFile); body.append("prior_file", priorFile);
  const { data } = await http.post(`/audit-engagements/${engagementId}/trial-balances/compare`, body);
  return data;
}

export async function uploadAuditLedger(engagementId: string, asOfDate: string, file: File): Promise<Record<string, unknown>> {
  const body = new FormData(); body.append("file", file);
  const { data } = await http.post(`/audit-engagements/${engagementId}/ledger`, body, { params: { as_of_date: asOfDate } });
  return data;
}

export async function listAgeingSchedules(engagementId: string): Promise<AuditAgeingSchedule[]> {
  const { data } = await http.get<AuditAgeingSchedule[]>(`/audit-engagements/${engagementId}/ageing-schedules`);
  return data;
}

export async function listAuditRiskFlags(engagementId: string): Promise<AuditRiskFlag[]> {
  const { data } = await http.get<AuditRiskFlag[]>(`/audit-engagements/${engagementId}/risk-flags`);
  return data;
}

export async function getStatutoryDues(engagementId: string): Promise<StatutoryDuesRow[]> {
  const { data } = await http.get<StatutoryDuesRow[]>(`/audit-engagements/${engagementId}/statutory-dues`);
  return data;
}

export async function recordStatutoryFiling(engagementId: string, payload: {
  tax_type: string; period: string; filing_status: string; filing_source: string;
  filed_at?: string; statutory_due_date?: string; source_reference?: string; notes?: string;
}): Promise<unknown> {
  const { data } = await http.post(`/audit-engagements/${engagementId}/statutory-dues`, payload);
  return data;
}

export async function reviewStatutoryFiling(engagementId: string, recordId: string): Promise<unknown> {
  const { data } = await http.post(`/audit-engagements/${engagementId}/statutory-dues/${recordId}/review`);
  return data;
}

export async function listTrialBalanceLines(uploadId: string): Promise<TrialBalanceLine[]> {
  const { data } = await http.get<TrialBalanceLine[]>(`/audit-engagements/trial-balances/${uploadId}/lines`);
  return data;
}

export async function listClientBankAccounts(clientId: string): Promise<ClientBankAccount[]> {
  const { data } = await http.get<ClientBankAccount[]>(`/clients/${clientId}/bank-accounts`);
  return data;
}
export async function createClientBankAccount(clientId: string, payload: { bank_name: string; branch_name?: string; account_name?: string; account_number: string; ifsc_code?: string }): Promise<ClientBankAccount> {
  const { data } = await http.post<ClientBankAccount>(`/clients/${clientId}/bank-accounts`, payload); return data;
}
export async function listBankConfirmations(engagementId: string): Promise<BankConfirmation[]> {
  const { data } = await http.get<BankConfirmation[]>(`/audit-engagements/${engagementId}/bank-confirmations`); return data;
}
export async function generateBankConfirmations(engagementId: string, asOfDate: string): Promise<BankConfirmation[]> {
  const { data } = await http.post<BankConfirmation[]>(`/audit-engagements/${engagementId}/bank-confirmations/generate`, { as_of_date: asOfDate }); return data;
}
export async function updateBankConfirmation(engagementId: string, id: string, action: string, payload?: Record<string, string>): Promise<BankConfirmation> {
  const { data } = await http.post<BankConfirmation>(`/audit-engagements/${engagementId}/bank-confirmations/${id}/${action}`, payload); return data;
}
export async function listAuditRatioAnalyses(engagementId: string): Promise<AuditRatioAnalysis[]> {
  const { data } = await http.get<AuditRatioAnalysis[]>(`/audit-engagements/${engagementId}/ratio-analyses`); return data;
}
export async function generateAuditRatioAnalysis(engagementId: string, payload: Record<string, string | string[]>): Promise<AuditRatioAnalysis> {
  const { data } = await http.post<AuditRatioAnalysis>(`/audit-engagements/${engagementId}/ratio-analyses`, payload); return data;
}
export async function updateAuditRatioAnalysis(engagementId: string, id: string, action: "review" | "approve"): Promise<AuditRatioAnalysis> {
  const { data } = await http.post<AuditRatioAnalysis>(`/audit-engagements/${engagementId}/ratio-analyses/${id}/${action}`); return data;
}

export async function createClient(payload: {
  name: string;
  gstins: { gstin: string; is_primary?: boolean }[];
  entity_type?: string;
  engagement_type?: string;
  contact_email?: string;
}): Promise<Client> {
  const { data } = await http.post<Client>("/clients", payload);
  return data;
}

export async function addGstin(clientId: string, gstin: string): Promise<ClientGSTIN> {
  const { data } = await http.post<ClientGSTIN>(`/clients/${clientId}/gstins`, {
    gstin,
    is_primary: false,
  });
  return data;
}

export async function listPeriods(clientId: string): Promise<PeriodInfo[]> {
  const { data } = await http.get<PeriodInfo[]>(`/clients/${clientId}/periods`);
  return data;
}

// ---------------------------------------------------------------------------
// Uploads
// ---------------------------------------------------------------------------

export async function uploadPurchaseRegister(
  gstinId: string,
  period: string,
  file: File,
  force = false,
): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await http.post<UploadResult>(
    `/gstins/${gstinId}/uploads/purchase-register`,
    form,
    { params: { period, force }, headers: { "Content-Type": "multipart/form-data" } },
  );
  return data;
}

export async function uploadGstr2b(
  gstinId: string,
  period: string,
  file: File,
): Promise<FetchResult> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await http.post<FetchResult>(`/gstins/${gstinId}/gstr2b/upload`, form, {
    params: { period },
    headers: { "Content-Type": "multipart/form-data" },
  });
  return data;
}

export async function loadSampleGstr2b(
  gstinId: string,
  period: string,
): Promise<FetchResult> {
  const { data } = await http.post<FetchResult>(`/gstins/${gstinId}/gstr2b/sample`, null, {
    params: { period },
  });
  return data;
}

export async function listUploads(
  gstinId: string,
  period?: string,
): Promise<UploadRecord[]> {
  const { data } = await http.get<UploadRecord[]>(`/gstins/${gstinId}/uploads`, {
    params: period ? { period } : {},
  });
  return data;
}

// ---------------------------------------------------------------------------
// Reconciliation
// ---------------------------------------------------------------------------

export async function runReconciliation(
  gstinId: string,
  period: string,
): Promise<DashboardSummary> {
  const { data } = await http.post<DashboardSummary>(`/gstins/${gstinId}/reconcile`, null, {
    params: { period },
  });
  return data;
}

export async function getSummary(
  gstinId: string,
  period: string,
): Promise<DashboardSummary> {
  const { data } = await http.get<DashboardSummary>(
    `/gstins/${gstinId}/reconciliation/${period}/summary`,
  );
  return data;
}

export async function getExceptions(
  gstinId: string,
  period: string,
  matchStatus?: string,
): Promise<ReconciliationResult[]> {
  const { data } = await http.get<ReconciliationResult[]>(
    `/gstins/${gstinId}/reconciliation/${period}/exceptions`,
    { params: matchStatus ? { match_status: matchStatus } : {} },
  );
  return data;
}

export function exportUrl(gstinId: string, period: string): string {
  return `${API_BASE}/gstins/${gstinId}/reconciliation/${period}/export`;
}

/**
 * Downloads the export through the API client rather than a plain link.
 *
 * A bare <a href> cannot carry the Authorization header, so the request
 * would arrive unauthenticated and 401.
 */
export async function downloadExport(gstinId: string, period: string): Promise<void> {
  const response = await http.get(`/gstins/${gstinId}/reconciliation/${period}/export`, {
    responseType: "blob",
  });
  const blobUrl = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = `GSTR2B_Exceptions_${period}.xlsx`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(blobUrl);
}

// ---------------------------------------------------------------------------
// Exceptions
// ---------------------------------------------------------------------------

export async function listExceptionQueue(params: {
  client_gstin_id?: string;
  period?: string;
  status?: string;
  urgency?: string;
}): Promise<ExceptionListItem[]> {
  const { data } = await http.get<ExceptionListItem[]>("/exceptions", { params });
  return data;
}

export async function resolveException(
  exceptionId: string,
  notes: string,
): Promise<ExceptionListItem> {
  const { data } = await http.patch<ExceptionListItem>(
    `/exceptions/${exceptionId}/resolve`,
    { resolution_notes: notes },
  );
  return data;
}

export async function approveException(
  exceptionId: string,
  approved: boolean,
  reworkReason?: string,
): Promise<ExceptionListItem> {
  const { data } = await http.patch<ExceptionListItem>(
    `/exceptions/${exceptionId}/approve`,
    { approved, rework_reason: reworkReason },
  );
  return data;
}

export async function bulkResolve(
  exceptionIds: string[],
  notes: string,
): Promise<{ resolved: number }> {
  const { data } = await http.post("/exceptions/bulk/resolve", {
    exception_ids: exceptionIds,
    resolution_notes: notes,
  });
  return data;
}

export async function addExceptionComment(
  exceptionId: string,
  comment: string,
): Promise<unknown> {
  const { data } = await http.post(`/exceptions/${exceptionId}/comments`, { comment });
  return data;
}

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

export async function listNotifications(unreadOnly = false): Promise<AppNotification[]> {
  const { data } = await http.get<AppNotification[]>("/notifications", {
    params: { unread_only: unreadOnly },
  });
  return data;
}

export async function getUnreadCount(): Promise<number> {
  const { data } = await http.get<{ unread: number }>("/notifications/unread-count");
  return data.unread;
}

export async function markNotificationRead(id: string): Promise<AppNotification> {
  const { data } = await http.post<AppNotification>(`/notifications/${id}/read`);
  return data;
}

export async function markAllNotificationsRead(): Promise<{ message: string }> {
  const { data } = await http.post("/notifications/read-all");
  return data;
}

export async function getNotificationPreferences(): Promise<NotificationPreference[]> {
  const { data } = await http.get<NotificationPreference[]>("/notifications/preferences");
  return data;
}

export async function updateNotificationPreferences(
  preferences: { event_type: string; in_app: boolean; email: boolean }[],
): Promise<NotificationPreference[]> {
  const { data } = await http.put<NotificationPreference[]>(
    "/notifications/preferences",
    { preferences },
  );
  return data;
}

// ---------------------------------------------------------------------------
// Live GST portal access
// ---------------------------------------------------------------------------

export async function getGspStatus(gstinId: string): Promise<GspStatus> {
  const { data } = await http.get<GspStatus>(`/gstins/${gstinId}/gsp`);
  return data;
}

export async function verifyGstinLive(gstinId: string): Promise<GstinInfo> {
  const { data } = await http.post<GstinInfo>(`/gstins/${gstinId}/gsp/verify-gstin`);
  return data;
}

export async function requestConsentOtp(
  gstinId: string,
  username: string,
): Promise<OtpRequestResult> {
  const { data } = await http.post<OtpRequestResult>(
    `/gstins/${gstinId}/gsp/consent/request`,
    { username },
  );
  return data;
}

export async function verifyConsentOtp(
  gstinId: string,
  attemptId: string,
  otp: string,
): Promise<ConsentVerifyResult> {
  const { data } = await http.post<ConsentVerifyResult>(
    `/gstins/${gstinId}/gsp/consent/verify`,
    { attempt_id: attemptId, otp },
  );
  return data;
}

export async function disconnectGsp(gstinId: string): Promise<GspStatus> {
  const { data } = await http.delete<GspStatus>(`/gstins/${gstinId}/gsp/consent`);
  return data;
}

export async function fetchGstr2bLive(
  gstinId: string,
  period: string,
): Promise<GspFetchResult> {
  const { data } = await http.post<GspFetchResult>(`/gstins/${gstinId}/gsp/fetch`, null, {
    params: { period },
  });
  return data;
}

export async function listFetchAttempts(
  gstinId: string,
  period?: string,
): Promise<FetchAttempt[]> {
  const { data } = await http.get<FetchAttempt[]>(`/gstins/${gstinId}/gsp/attempts`, {
    params: period ? { period } : {},
  });
  return data;
}

// ---------------------------------------------------------------------------
// Notices
// ---------------------------------------------------------------------------

export async function listNotices(params: {
  clientId?: string;
  status?: string;
  openOnly?: boolean;
  assignedTo?: string;
}): Promise<NoticeSummary[]> {
  const { data } = await http.get<NoticeSummary[]>("/notices", {
    params: {
      client_id: params.clientId,
      status: params.status,
      open_only: params.openOnly,
      assigned_to: params.assignedTo,
    },
  });
  return data;
}

export async function getNoticeStats(): Promise<NoticeStats> {
  const { data } = await http.get<NoticeStats>("/notices/stats");
  return data;
}

export async function getNoticeTypes(): Promise<NoticeTypeOption[]> {
  const { data } = await http.get<NoticeTypeOption[]>("/notices/types");
  return data;
}

export async function uploadNotice(payload: {
  file: File;
  clientId: string;
  clientGstinId?: string;
  title?: string;
}): Promise<NoticeDetail> {
  const form = new FormData();
  form.append("file", payload.file);
  form.append("client_id", payload.clientId);
  if (payload.clientGstinId) form.append("client_gstin_id", payload.clientGstinId);
  if (payload.title) form.append("title", payload.title);
  // Analysis can take a while when a real model is reading the notice.
  const { data } = await http.post<NoticeDetail>("/notices", form, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 180_000,
  });
  return data;
}

export async function getNotice(id: string): Promise<NoticeDetail> {
  const { data } = await http.get<NoticeDetail>(`/notices/${id}`);
  return data;
}

export async function updateNotice(id: string, body: NoticeUpdate): Promise<NoticeDetail> {
  const { data } = await http.patch<NoticeDetail>(`/notices/${id}`, body);
  return data;
}

export async function deleteNotice(id: string): Promise<void> {
  await http.delete(`/notices/${id}`);
}

export async function putNoticeText(id: string, text: string): Promise<NoticeDetail> {
  const { data } = await http.put<NoticeDetail>(`/notices/${id}/text`, { text });
  return data;
}

export async function analyseNotice(id: string, overwrite = false): Promise<NoticeDetail> {
  const { data } = await http.post<NoticeDetail>(
    `/notices/${id}/analyse`,
    { overwrite },
    { timeout: 180_000 },
  );
  return data;
}

export async function getNoticeEvidence(id: string): Promise<NoticeEvidence> {
  const { data } = await http.get<NoticeEvidence>(`/notices/${id}/evidence`);
  return data;
}

export async function generateNoticeDraft(id: string, replace = false): Promise<NoticeDetail> {
  const { data } = await http.post<NoticeDetail>(
    `/notices/${id}/draft/generate`,
    { replace },
    { timeout: 180_000 },
  );
  return data;
}

export async function saveNoticeDraft(id: string, text: string): Promise<NoticeDetail> {
  const { data } = await http.put<NoticeDetail>(`/notices/${id}/draft`, { text });
  return data;
}

export async function transitionNotice(
  id: string,
  body: {
    to_status: string;
    reason?: string;
    filing_reference?: string;
    filed_at?: string;
    outcome?: string;
  },
): Promise<NoticeDetail> {
  const { data } = await http.post<NoticeDetail>(`/notices/${id}/transition`, body);
  return data;
}

export async function getNoticeHistory(id: string): Promise<NoticeHistoryEntry[]> {
  const { data } = await http.get<NoticeHistoryEntry[]>(`/notices/${id}/history`);
  return data;
}

/** Downloads the decrypted original. Goes through the API client so the
 *  Authorization header is sent; a bare link would arrive unauthenticated. */
export async function downloadNoticeFile(id: string, filename: string): Promise<void> {
  const response = await http.get(`/notices/${id}/file`, { responseType: "blob" });
  const blobUrl = window.URL.createObjectURL(new Blob([response.data]));
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = filename || "notice";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(blobUrl);
}

// ---------------------------------------------------------------------------
// Portfolio (firm-wide reconciliation rollup)
// ---------------------------------------------------------------------------

export async function getPortfolio(period?: string): Promise<Portfolio> {
  const { data } = await http.get<Portfolio>("/portfolio", { params: period ? { period } : {} });
  return data;
}

export async function getPortfolioPeriods(): Promise<{ periods: string[]; default: string }> {
  const { data } = await http.get("/portfolio/periods");
  return data;
}

// ---------------------------------------------------------------------------
// Pre-audit brief
// ---------------------------------------------------------------------------

export async function getAuditBrief(
  clientId: string,
  params: { asOf?: string; lookback?: number } = {},
): Promise<AuditBrief> {
  const { data } = await http.get<AuditBrief>(`/clients/${clientId}/audit-brief`, {
    params: { as_of: params.asOf, lookback: params.lookback },
  });
  return data;
}

export default http;
