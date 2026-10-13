import type { MatchStatus } from "./types";

export const STATUS_LABELS: Record<MatchStatus, string> = {
  MATCHED: "Matched",
  FORMATTING_MISMATCH: "Formatting mismatch (auto-resolved)",
  SUPPLIER_NOT_FILED: "Supplier not filed",
  VALUE_MISMATCH: "Value mismatch",
  CREDIT_NOTE_DISCREPANCY: "Credit note discrepancy",
  MISSING_IN_BOOKS: "Missing in books",
};

export const STATUS_CLASSES: Record<MatchStatus, string> = {
  MATCHED: "bg-emerald-50 text-emerald-700 border-emerald-200",
  FORMATTING_MISMATCH: "bg-sky-50 text-sky-700 border-sky-200",
  SUPPLIER_NOT_FILED: "bg-amber-50 text-amber-800 border-amber-200",
  VALUE_MISMATCH: "bg-rose-50 text-rose-700 border-rose-200",
  CREDIT_NOTE_DISCREPANCY: "bg-rose-50 text-rose-700 border-rose-200",
  MISSING_IN_BOOKS: "bg-violet-50 text-violet-700 border-violet-200",
};

export function formatINR(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  }).format(value);
}
