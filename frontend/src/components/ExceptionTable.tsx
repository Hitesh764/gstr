import { useMemo, useState } from "react";
import type { MatchStatus, ReconciliationResult } from "../types";
import { STATUS_LABELS } from "../statusMeta";
import ExceptionRow from "./ExceptionRow";

interface Props {
  results: ReconciliationResult[];
  onResolve: (exceptionId: string, note: string) => Promise<void>;
  onApprove: (exceptionId: string, approved: boolean, reworkReason?: string) => Promise<void>;
}

type SortKey = "vendor" | "taxable_value" | "confidence_score" | "match_status";

const ALL_STATUSES: MatchStatus[] = [
  "FORMATTING_MISMATCH",
  "SUPPLIER_NOT_FILED",
  "VALUE_MISMATCH",
  "CREDIT_NOTE_DISCREPANCY",
  "MISSING_IN_BOOKS",
];

// Only an approved or auto-resolved item counts as settled.
const SETTLED = new Set(["APPROVED", "AUTO_RESOLVED"]);

export default function ExceptionTable({ results, onResolve, onApprove }: Props) {
  const [statusFilter, setStatusFilter] = useState<string>("ALL");
  const [showSettled, setShowSettled] = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>("match_status");
  const [sortDir, setSortDir] = useState<1 | -1>(1);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    let rows = results;
    if (statusFilter !== "ALL") rows = rows.filter((r) => r.match_status === statusFilter);
    if (!showSettled) {
      rows = rows.filter((r) => !r.exception || !SETTLED.has(r.exception.status));
    }

    const vendorOf = (r: ReconciliationResult) =>
      r.purchase_entry?.vendor_name ?? r.gstr2b_entry?.vendor_name ?? "";
    const valueOf = (r: ReconciliationResult) =>
      Number(r.purchase_entry?.taxable_value ?? r.gstr2b_entry?.taxable_value ?? 0);

    return [...rows].sort((a, b) => {
      let cmp = 0;
      if (sortKey === "vendor") cmp = vendorOf(a).localeCompare(vendorOf(b));
      else if (sortKey === "taxable_value") cmp = valueOf(a) - valueOf(b);
      else if (sortKey === "confidence_score") cmp = a.confidence_score - b.confidence_score;
      else cmp = a.match_status.localeCompare(b.match_status);
      return cmp * sortDir;
    });
  }, [results, statusFilter, showSettled, sortKey, sortDir]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === 1 ? -1 : 1));
    else {
      setSortKey(key);
      setSortDir(1);
    }
  }

  const headerBtn = (key: SortKey, label: string) => (
    <button
      onClick={() => toggleSort(key)}
      className="flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-slate-500 hover:text-slate-700"
    >
      {label}
      {sortKey === key && <span>{sortDir === 1 ? "▲" : "▼"}</span>}
    </button>
  );

  return (
    <div className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex flex-wrap items-center gap-3 border-b border-slate-200 p-3">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        >
          <option value="ALL">All exception types</option>
          {ALL_STATUSES.map((s) => (
            <option key={s} value={s}>
              {STATUS_LABELS[s]}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          <input
            type="checkbox"
            checked={showSettled}
            onChange={(e) => setShowSettled(e.target.checked)}
          />
          Show settled
        </label>
        <span className="ml-auto text-sm text-slate-500">{filtered.length} exceptions</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-180 border-collapse">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50">
              <th className="px-3 py-2 text-left">{headerBtn("match_status", "Status")}</th>
              <th className="px-3 py-2 text-left">{headerBtn("vendor", "Vendor")}</th>
              <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-500">
                Invoice #
              </th>
              <th className="px-3 py-2 text-right">
                {headerBtn("taxable_value", "Taxable value")}
              </th>
              <th className="px-3 py-2 text-right">
                {headerBtn("confidence_score", "Confidence")}
              </th>
              <th className="px-3 py-2 text-center text-xs font-semibold uppercase tracking-wide text-slate-500">
                Workflow
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr>
                <td colSpan={6} className="px-3 py-8 text-center text-sm text-slate-400">
                  No exceptions match this filter.
                </td>
              </tr>
            )}
            {filtered.map((r) => (
              <ExceptionRow
                key={r.id}
                result={r}
                expanded={expandedId === r.id}
                onToggle={() => setExpandedId(expandedId === r.id ? null : r.id)}
                onResolve={onResolve}
                onApprove={onApprove}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
