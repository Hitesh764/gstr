import type { NoticeStatus, NoticeSummary, NoticeUrgency } from "./types";

export const STATUS_LABEL: Record<NoticeStatus, string> = {
  RECEIVED: "Received",
  UNDER_REVIEW: "Under review",
  RESPONSE_DRAFTED: "Response drafted",
  PARTNER_APPROVED: "Partner approved",
  FILED: "Filed",
  CLOSED: "Closed",
};

export const STATUS_CLASS: Record<NoticeStatus, string> = {
  RECEIVED: "border-slate-200 bg-slate-50 text-slate-700",
  UNDER_REVIEW: "border-sky-200 bg-sky-50 text-sky-800",
  RESPONSE_DRAFTED: "border-amber-200 bg-amber-50 text-amber-800",
  PARTNER_APPROVED: "border-emerald-200 bg-emerald-50 text-emerald-800",
  FILED: "border-emerald-300 bg-emerald-100 text-emerald-900",
  CLOSED: "border-slate-200 bg-slate-100 text-slate-600",
};

/** The pipeline in order. The send-back from RESPONSE_DRAFTED is not a step. */
export const PIPELINE: NoticeStatus[] = [
  "RECEIVED",
  "UNDER_REVIEW",
  "RESPONSE_DRAFTED",
  "PARTNER_APPROVED",
  "FILED",
  "CLOSED",
];

export const URGENCY_CLASS: Record<NoticeUrgency, string> = {
  overdue: "border-rose-300 bg-rose-50 text-rose-800",
  due_today: "border-rose-300 bg-rose-50 text-rose-800",
  critical: "border-rose-200 bg-rose-50 text-rose-700",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  ok: "border-emerald-200 bg-emerald-50 text-emerald-700",
  none: "border-slate-200 bg-slate-50 text-slate-600",
};

export function deadlineText(n: Pick<NoticeSummary, "urgency" | "days_until_deadline" | "response_deadline">): string {
  if (!n.response_deadline) return "No deadline set";
  const d = n.days_until_deadline;
  if (d === null || d === undefined) return "Due " + n.response_deadline;
  if (d < 0) return `${-d} day${d === -1 ? "" : "s"} overdue`;
  if (d === 0) return "Due today";
  return `${d} day${d === 1 ? "" : "s"} left`;
}

/** The label on a button that moves a notice to `to`, given where it is now. */
export function actionLabel(current: NoticeStatus, to: NoticeStatus): string {
  if (to === "UNDER_REVIEW") return current === "RECEIVED" ? "Start review" : "Send back for changes";
  if (to === "RESPONSE_DRAFTED") return "Submit for approval";
  if (to === "PARTNER_APPROVED") return "Approve response";
  if (to === "FILED") return "Record as filed";
  return "Close notice";
}

export const AUDIT_LABEL: Record<string, string> = {
  NOTICE_UPLOADED: "Uploaded",
  NOTICE_UPDATED: "Details edited",
  NOTICE_ANALYSED: "Read by the analyser",
  NOTICE_TEXT_EDITED: "Notice text entered by hand",
  NOTICE_ASSIGNED: "Assigned",
  NOTICE_DRAFT_GENERATED: "Draft generated from reconciliation records",
  NOTICE_DRAFT_EDITED: "Draft edited",
  NOTICE_STATUS_CHANGED: "Status changed",
  NOTICE_FILE_DOWNLOADED: "Original downloaded",
  NOTICE_DELETED: "Deleted",
};

/** Indian digit grouping, for a decimal string such as "180000.00". */
export function formatRupees(value?: string | null): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return value;
  return "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** "2025-04" -> "Apr 2025". */
export function formatPeriod(p?: string | null): string {
  if (!p) return "—";
  const [y, m] = p.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

export function periodRange(n: Pick<NoticeSummary, "period" | "period_to">): string {
  if (!n.period) return "—";
  if (!n.period_to || n.period_to === n.period) return formatPeriod(n.period);
  return `${formatPeriod(n.period)} to ${formatPeriod(n.period_to)}`;
}
