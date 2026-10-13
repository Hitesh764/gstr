export type MatchStatus =
  | "MATCHED"
  | "FORMATTING_MISMATCH"
  | "SUPPLIER_NOT_FILED"
  | "VALUE_MISMATCH"
  | "CREDIT_NOTE_DISCREPANCY"
  | "MISSING_IN_BOOKS";

export type UserRole =
  | "OWNER"
  | "PARTNER"
  | "MANAGER"
  | "ASSOCIATE"
  | "CLIENT_VIEWER";

export type ExceptionStatus =
  | "OPEN"
  | "IN_PROGRESS"
  | "PENDING_APPROVAL"
  | "APPROVED"
  | "REWORK"
  | "AUTO_RESOLVED";

export type Urgency = "lapsed" | "critical" | "warning" | "ok" | "none";

export interface User {
  id: string;
  firm_id: string;
  name: string;
  email: string;
  phone?: string | null;
  role: UserRole;
  is_active: boolean;
  email_verified: boolean;
  totp_enabled: boolean;
  scoped_client_id?: string | null;
  last_login_at?: string | null;
  created_at: string;
}

export interface Firm {
  id: string;
  name: string;
  contact_email: string;
  contact_phone?: string | null;
  gstin?: string | null;
  pan?: string | null;
  subscription_tier: string;
  subscription_status: string;
  require_2fa_for_partners: boolean;
  created_at: string;
}

export interface AuthSession {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
  firm: Firm;
}

export interface ClientGSTIN {
  id: string;
  client_id: string;
  gstin: string;
  state_code?: string | null;
  state_name?: string | null;
  legal_name?: string | null;
  trade_name?: string | null;
  is_primary: boolean;
  verified_at?: string | null;
  consent_granted_at?: string | null;
  consent_expires_at?: string | null;
  has_active_consent: boolean;
  gsp_state: GspState;
  gsp_provider?: string | null;
}

export interface Client {
  id: string;
  firm_id: string;
  name: string;
  pan?: string | null;
  entity_type: string;
  engagement_type: string;
  status: string;
  contact_name?: string | null;
  contact_email?: string | null;
  contact_phone?: string | null;
  assigned_associate_id?: string | null;
  reconciliation_frequency: string;
  created_at: string;
  gstins: ClientGSTIN[];
}

export type AuditEngagementStatus = "PLANNING" | "FIELDWORK" | "COMPLETION" | "REPORTING" | "CLOSED";
export interface AuditEngagement {
  id: string;
  client_id: string;
  financial_year: string;
  engagement_type: "STATUTORY" | "TAX" | "INTERNAL";
  status: AuditEngagementStatus;
    engagement_partner_id: string;
    reporting_deadline?: string | null;
  created_by: string;
  materiality_amount?: string | null;
  materiality_basis?: string | null;
  materiality_confirmed_by?: string | null;
  caro_applicable?: boolean | null;
  caro_applicability_reasoning?: string | null;
  caro_assessment_id?: string | null;
  caro_confirmed_by?: string | null;
}

export interface ManagementRepLetter {
  id: string; engagement_id: string; version: number; template_version: string;
  draft_content: string; final_content?: string | null; status: string;
  prepared_by: string; reviewed_by?: string | null; approved_by?: string | null;
  sent_to_client_at?: string | null; received_signed_at?: string | null;
}
export interface AuditWorkingPaper {
  id: string; engagement_id: string; version: number; title: string; sa_reference: string;
  content: string; cross_references: Array<{ type: string; id: string }>;
  status: string; prepared_by: string; reviewed_by?: string | null; approved_by?: string | null;
}
export interface AuditProcedure {
  id: string; engagement_id: string; sa_reference: string; procedure_name: string; status: string;
  performed_by: string; performed_at?: string | null; evidence_file_paths: string[];
  conclusion_notes?: string | null; reviewed_by?: string | null; approved_by?: string | null;
}
export interface AuditDashboardItem {
  id: string; client_name: string; financial_year: string; engagement_type: string; status: string;
  reporting_deadline?: string | null; days_until_deadline?: number | null; open_risk_flags: number;
  high_severity_flags: number; caro_total: number; caro_finalized: number; bank_confirmations_outstanding: number;
}

export interface RelatedParty {
  id: string;
  client_id: string;
  name: string;
  aliases: string[];
  pan?: string | null;
  relationship_nature: string;
  effective_from?: string | null;
  effective_to?: string | null;
  is_active: boolean;
  created_by: string;
  updated_by: string;
}

export type CAROResponseStatus = "DRAFT" | "MANAGER_REVIEWED" | "FINALIZED" | "SUPERSEDED";
export interface CAROClauseResponse {
  id: string;
  engagement_id: string;
  clause_definition_id: string;
  clause_number: number;
  version: number;
  applicability: string;
  applicability_reasoning: string;
  draft_narrative: string;
  final_narrative?: string | null;
  source_snapshot: Record<string, unknown>;
  status: CAROResponseStatus;
  prepared_by: string;
  reviewed_by?: string | null;
  review_notes?: string | null;
  finalized_by?: string | null;
}

export interface CAROApplicabilityAssessment {
  id: string;
  engagement_id: string;
  status: string;
  determined_applicable: boolean;
  applicable_clause_numbers: number[];
  reasoning: string;
  confirmed_applicable?: boolean | null;
  confirmed_by?: string | null;
}

export interface TrialBalanceUpload {
  id: string; engagement_id: string; period_type: string; version: number; is_current: boolean;
  row_count: number; original_filename: string; uploaded_at: string;
}
export interface AuditRiskFlag {
  id: string; flag_type: string; severity: string; system_note: string; status: string;
  source_reference_key?: string | null; created_at: string;
}
export interface AuditAgeingSchedule {
  id: string; schedule_type: string; as_of_date: string; source_version: number;
  data: { bucket_totals?: Record<string, string>; oldest_bucket_reviews?: Array<Record<string, unknown>>; [key: string]: unknown };
}
export interface StatutoryDuesRow {
  period: string; tax_type: "GST" | "TDS" | "PF" | "ESI"; filing_status?: string | null;
  verification_status: string; current_record?: { id: string; recorded_by: string; source_reference?: string | null; notes?: string | null; reviewed_by?: string | null } | null;
  gstr2b_context: Array<{ gstin: string; gstr2b_available: boolean; reconciliation_available: boolean; scope_note: string }>;
}
export interface TrialBalanceLine { id: string; upload_id: string; account_code: string; account_name: string; account_group?: string | null; current_year_amount?: string | number | null; prior_year_amount?: string | number | null; }
export interface ClientBankAccount { id: string; client_id: string; bank_name: string; branch_name?: string | null; account_name?: string | null; masked_account_number: string; ifsc_code?: string | null; is_active: boolean; }
export interface BankConfirmation { id: string; bank_account_id: string; drafted_by: string; account_number_last4: string; version: number; as_of_date: string; letter_status: string; draft_content: string; approved_by?: string | null; books_balance?: string | null; confirmed_balance?: string | null; discrepancy_amount?: string | null; discrepancy_notes?: string | null; }
export interface AuditRatioAnalysis { id: string; version: number; status: string; prepared_by: string; reviewed_by?: string | null; approved_by?: string | null; account_mapping: Record<string, unknown>; ratio_results: { ratios: Record<string, { current_year?: string | null; prior_year?: string | null; movement_pct?: string | null; threshold_exceeded: boolean; risk_flag_id?: string | null; unavailable_reason?: string | null }> }; risk_flag_ids: string[]; }

export interface PurchaseEntry {
  id: string;
  vendor_name: string;
  vendor_gstin: string;
  invoice_number: string;
  invoice_date: string;
  taxable_value: number;
  cgst: number;
  sgst: number;
  igst: number;
  total_value: number;
}

export interface GSTR2BEntry {
  id: string;
  vendor_name?: string | null;
  vendor_gstin: string;
  invoice_number: string;
  invoice_date: string;
  taxable_value: number;
  cgst: number;
  sgst: number;
  igst: number;
  total_value: number;
  filing_status: string;
}

export interface ExceptionSummary {
  id: string;
  status: ExceptionStatus;
  assigned_to?: string | null;
  resolution_notes?: string | null;
  resolved_by?: string | null;
  resolved_at?: string | null;
  approved_by?: string | null;
  approved_at?: string | null;
  rework_reason?: string | null;
  itc_lapse_deadline?: string | null;
}

export interface ExceptionListItem extends ExceptionSummary {
  client_gstin_id: string;
  period: string;
  days_until_lapse?: number | null;
  urgency: Urgency;
  created_at: string;
}

export interface ReconciliationResult {
  id: string;
  run_id: string;
  match_status: MatchStatus;
  confidence_score: number;
  plain_language_note?: string | null;
  match_key?: string | null;
  purchase_entry?: PurchaseEntry | null;
  gstr2b_entry?: GSTR2BEntry | null;
  exception?: ExceptionSummary | null;
}

export interface DashboardSummary {
  period: string;
  client_gstin_id: string;
  run_id?: string | null;
  total_itc_in_books: number;
  total_itc_eligible_2b: number;
  total_itc_blocked_2b: number;
  total_itc_at_risk: number;
  pct_auto_matched: number;
  counts_by_status: Record<string, number>;
  total_entries: number;
  exceptions_open: number;
  exceptions_pending_approval: number;
}

// ---------------------------------------------------------------------------
// Portfolio (firm-wide reconciliation rollup)
// ---------------------------------------------------------------------------

export type PortfolioStatus = "NEEDS_REVIEW" | "AWAITING_APPROVAL" | "NEEDS_DATA" | "NOT_STARTED" | "CLEAN";

export interface PortfolioRow {
  client_id: string;
  client_name: string;
  client_status: string;
  assigned_associate_id?: string | null;
  assigned_associate_name?: string | null;
  gstin_id: string;
  gstin: string;
  state_name?: string | null;
  has_purchase_register: boolean;
  has_gstr2b: boolean;
  run_version?: number | null;
  run_completed_at?: string | null;
  exceptions_open: number;
  exceptions_pending_approval: number;
  exceptions_rework: number;
  /** A decimal string (tax, not taxable value), so no precision is lost. */
  itc_at_risk: string;
  nearest_itc_lapse_deadline?: string | null;
  status: PortfolioStatus;
}

export interface Portfolio {
  period: string;
  total_clients: number;
  needs_review: number;
  awaiting_approval: number;
  needs_data: number;
  not_started: number;
  clean: number;
  total_itc_at_risk: string;
  rows: PortfolioRow[];
}

// ---------------------------------------------------------------------------
// Pre-audit brief
// ---------------------------------------------------------------------------

export interface AuditBriefPoint {
  period: string;
  reconciled: boolean;
  invoice_count: number;
  matched_count: number;
  match_rate: number | null;
  itc_claimed: string;
  itc_eligible: string;
  itc_blocked: string;
  itc_at_risk: string;
}

export interface AuditBriefVendor {
  vendor_gstin: string;
  vendor_name?: string | null;
  itc_claimed: string;
  share: number;
}

export interface AuditBriefRecurringVendor {
  vendor_gstin: string;
  vendor_name?: string | null;
  periods_flagged: string[];
  statuses: string[];
}

export interface AuditBriefStaleException {
  exception_id: string;
  period: string;
  status: string;
  age_days: number;
  vendor_name?: string | null;
  invoice_number?: string | null;
}

export interface AuditBriefRedFlag {
  severity: "critical" | "warning";
  message: string;
}

export interface AuditBrief {
  client_id: string;
  client_name: string;
  as_of: string;
  lookback_periods: string[];
  points: AuditBriefPoint[];
  filing_consistency: number;
  reconciled_periods: number;
  total_periods: number;
  latest_reconciled_period?: string | null;
  latest_itc_at_risk: string;
  vendor_concentration: AuditBriefVendor[];
  recurring_vendors: AuditBriefRecurringVendor[];
  stale_exceptions: AuditBriefStaleException[];
  open_exception_count: number;
  red_flags: AuditBriefRedFlag[];
  generated_at: string;
  scope_note: string;
}

export interface RowError {
  row?: number | null;
  field?: string | null;
  message: string;
  severity: string;
}

export interface UploadRecord {
  id: string;
  client_gstin_id: string;
  period: string;
  version: number;
  is_current: boolean;
  source: string;
  original_filename?: string | null;
  status: string;
  row_count: number;
  error_count: number;
  uploaded_at: string;
  uploaded_by?: string | null;
}

export interface UploadResult {
  upload?: UploadRecord | null;
  inserted: number;
  errors: RowError[];
  warnings: RowError[];
  accepted: boolean;
}

export interface FetchRecord {
  id: string;
  client_gstin_id: string;
  period: string;
  version: number;
  is_current: boolean;
  source: string;
  gsp_provider?: string | null;
  generated_on?: string | null;
  fetched_at: string;
}

export interface FetchResult {
  fetch?: FetchRecord | null;
  inserted: number;
  errors: RowError[];
}

export interface PeriodInfo {
  period: string;
  gstin_ids: string[];
  has_purchase_register: boolean;
  has_gstr2b: boolean;
}

export interface AuditEntry {
  id: string;
  user_id?: string | null;
  action: string;
  entity_type?: string | null;
  entity_id?: string | null;
  meta?: unknown;
  ip_address?: string | null;
  created_at: string;
}

export interface AppNotification {
  id: string;
  type: string;
  title: string;
  message?: string | null;
  link?: string | null;
  read_status: boolean;
  entity_type?: string | null;
  entity_id?: string | null;
  created_at: string;
}

export interface NotificationPreference {
  event_type: string;
  label: string;
  description: string;
  in_app: boolean;
  email: boolean;
}

export type GspState = "NONE" | "ACTIVE" | "EXPIRING" | "EXPIRED";

export interface FetchAttempt {
  id: string;
  period: string;
  trigger: "SCHEDULED" | "MANUAL";
  status: "SUCCESS" | "RETRYING" | "FAILED" | "SKIPPED";
  attempt_number: number;
  error_code?: string | null;
  message?: string | null;
  next_retry_at?: string | null;
  invoice_count?: number | null;
  credit_note_count?: number | null;
  created_at: string;
}

export interface GspStatus {
  gstin_id: string;
  gstin: string;
  active_provider: string;
  connected_provider?: string | null;
  provider_is_mock: boolean;
  state: GspState;
  portal_username?: string | null;
  granted_at?: string | null;
  session_expires_at?: string | null;
  token_expires_at?: string | null;
  last_refreshed_at?: string | null;
  seconds_remaining?: number | null;
  window_note?: string | null;
  renew_before_fetch_window?: string | null;
  next_fetch_period: string;
  recent_attempts: FetchAttempt[];
}

export interface OtpRequestResult {
  attempt_id: string;
  message: string;
  expires_at: string;
  provider: string;
}

export interface ConsentVerifyResult {
  status: GspStatus;
  window_note?: string | null;
}

export interface GspFetchResult {
  status: "SUCCESS" | "FAILED" | "SKIPPED" | "RETRY";
  message: string;
  error_code?: string | null;
  fetch_id?: string | null;
  run_id?: string | null;
  invoice_count?: number | null;
  credit_note_count?: number | null;
  reconciled: boolean;
  warnings: string[];
}

export interface GstinInfo {
  gstin: string;
  legal_name?: string | null;
  trade_name?: string | null;
  status: string;
  is_active: boolean;
  state_name?: string | null;
  warning?: string | null;
}

// ---------------------------------------------------------------------------
// Notices
// ---------------------------------------------------------------------------

export type NoticeStatus =
  | "RECEIVED"
  | "UNDER_REVIEW"
  | "RESPONSE_DRAFTED"
  | "PARTNER_APPROVED"
  | "FILED"
  | "CLOSED";

export type NoticeUrgency = "overdue" | "due_today" | "critical" | "warning" | "ok" | "none";

export type NoticeExtractionStatus = "PENDING" | "DONE" | "NEEDS_OCR" | "FAILED" | "MANUAL";
export type NoticeAnalysisStatus = "NOT_RUN" | "DONE" | "FAILED";

export interface NoticeSummary {
  id: string;
  client_id: string;
  client_name?: string | null;
  client_gstin_id?: string | null;
  gstin?: string | null;
  title?: string | null;
  notice_type?: string | null;
  notice_type_label?: string | null;
  notice_reference?: string | null;
  section?: string | null;
  notice_date?: string | null;
  response_deadline?: string | null;
  deadline_confirmed: boolean;
  days_until_deadline?: number | null;
  urgency: NoticeUrgency;
  period?: string | null;
  period_to?: string | null;
  /** A decimal string, so no precision is lost. */
  amount_in_question?: string | null;
  status: NoticeStatus;
  assigned_to?: string | null;
  assigned_to_name?: string | null;
  extraction_status: NoticeExtractionStatus;
  analysis_status: NoticeAnalysisStatus;
  original_filename?: string | null;
  created_at: string;
  updated_at: string;
}

export interface NoticeAction {
  to: NoticeStatus;
  allowed: boolean;
  blocked_reason?: string | null;
}

export interface NoticeFactsReading {
  notice_type: string;
  title?: string | null;
  reference?: string | null;
  section?: string | null;
  notice_date?: string | null;
  response_deadline?: string | null;
  deadline_basis?: string | null;
  period_from?: string | null;
  period_to?: string | null;
  amount?: string | null;
  gstin?: string | null;
  issuing_authority?: string | null;
  questions: string[];
  summary: string;
  confidence: "high" | "medium" | "low";
  warnings: string[];
}

export interface NoticeDraftMeta {
  provider: string;
  model: string;
  generated_at: string;
  cited_evidence_ids: string[];
  open_points: string[];
  unverified_amounts: string[];
  evidence_gaps: string[];
  evidence_count: number;
}

export interface NoticeAnalysis {
  provider?: string;
  model?: string;
  analysed_at?: string;
  facts?: NoticeFactsReading;
  draft?: NoticeDraftMeta;
}

export interface NoticeDetail extends NoticeSummary {
  ocr_text?: string | null;
  extraction_method?: string | null;
  extraction_message?: string | null;
  page_count?: number | null;
  content_type?: string | null;
  size_bytes?: number | null;
  file_available: boolean;
  ai_analysis?: NoticeAnalysis | null;
  analysis_error?: string | null;
  analysed_at?: string | null;
  drafted_response?: string | null;
  drafted_by?: string | null;
  drafted_by_name?: string | null;
  drafted_at?: string | null;
  submitted_by?: string | null;
  submitted_by_name?: string | null;
  submitted_at?: string | null;
  rework_reason?: string | null;
  approved_by?: string | null;
  approved_by_name?: string | null;
  approved_at?: string | null;
  filed_by?: string | null;
  filed_by_name?: string | null;
  filed_at?: string | null;
  filing_reference?: string | null;
  outcome?: string | null;
  closed_at?: string | null;
  uploaded_by_name?: string | null;
  actions: NoticeAction[];
  editable: { facts: boolean; draft: boolean; deadline: boolean; text: boolean };
}

export interface NoticeStats {
  open: number;
  overdue: number;
  due_within_7_days: number;
  awaiting_approval: number;
  unassigned: number;
  no_deadline: number;
}

export interface NoticeTypeOption {
  code: string;
  label: string;
}

export interface NoticeEvidenceItem {
  id: string;
  kind: "RUN_SUMMARY" | "EXCEPTION";
  text: string;
}

export interface NoticeEvidence {
  gstin?: string | null;
  periods: string[];
  gaps: string[];
  items: NoticeEvidenceItem[];
}

export interface NoticeHistoryEntry {
  id: string;
  action: string;
  user_id?: string | null;
  user_name?: string | null;
  created_at: string;
  details?: Record<string, unknown> | null;
}

export interface NoticeUpdate {
  title?: string | null;
  notice_type?: string | null;
  notice_reference?: string | null;
  section?: string | null;
  notice_date?: string | null;
  response_deadline?: string | null;
  period?: string | null;
  period_to?: string | null;
  amount_in_question?: string | null;
  client_gstin_id?: string | null;
  assigned_to?: string | null;
  confirm_deadline?: boolean;
}
