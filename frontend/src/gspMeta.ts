import type { FetchAttempt, GspState } from "./types";

export const STATE_LABEL: Record<GspState, string> = {
  NONE: "Not connected",
  ACTIVE: "Connected",
  EXPIRING: "Expiring soon",
  EXPIRED: "Expired",
};

export const STATE_CLASS: Record<GspState, string> = {
  NONE: "border-slate-200 bg-slate-50 text-slate-600",
  ACTIVE: "border-emerald-200 bg-emerald-50 text-emerald-700",
  EXPIRING: "border-amber-200 bg-amber-50 text-amber-800",
  EXPIRED: "border-rose-200 bg-rose-50 text-rose-700",
};

export const ATTEMPT_CLASS: Record<FetchAttempt["status"], string> = {
  SUCCESS: "text-emerald-700",
  RETRYING: "text-amber-700",
  FAILED: "text-rose-700",
  SKIPPED: "text-slate-500",
};
