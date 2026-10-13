import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getErrorMessage, getPortfolio, getPortfolioPeriods } from "../api/client";
import { formatINR } from "../statusMeta";
import type { PortfolioRow, PortfolioStatus } from "../types";
import { formatDate } from "../utils/time";

const STATUS_LABEL: Record<PortfolioStatus, string> = {
  NEEDS_REVIEW: "Needs review",
  AWAITING_APPROVAL: "Awaiting approval",
  NEEDS_DATA: "Data uploaded, not reconciled",
  NOT_STARTED: "Nothing uploaded yet",
  CLEAN: "Clean",
};

const STATUS_CLASS: Record<PortfolioStatus, string> = {
  NEEDS_REVIEW: "border-rose-200 bg-rose-50 text-rose-700",
  AWAITING_APPROVAL: "border-amber-200 bg-amber-50 text-amber-800",
  NEEDS_DATA: "border-sky-200 bg-sky-50 text-sky-800",
  NOT_STARTED: "border-slate-200 bg-slate-50 text-slate-600",
  CLEAN: "border-emerald-200 bg-emerald-50 text-emerald-700",
};

const STATUS_FILTERS: PortfolioStatus[] = [
  "NEEDS_REVIEW",
  "AWAITING_APPROVAL",
  "NEEDS_DATA",
  "NOT_STARTED",
  "CLEAN",
];

function formatPeriod(p: string): string {
  const [y, m] = p.split("-").map(Number);
  if (!y || !m) return p;
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

export default function PortfolioPage() {
  const [period, setPeriod] = useState<string | undefined>(undefined);
  const [status, setStatus] = useState<string>("");
  const navigate = useNavigate();

  const periodsQuery = useQuery({ queryKey: ["portfolio-periods"], queryFn: getPortfolioPeriods });
  const effectivePeriod = period ?? periodsQuery.data?.default;

  const portfolioQuery = useQuery({
    queryKey: ["portfolio", effectivePeriod],
    queryFn: () => getPortfolio(effectivePeriod),
    enabled: Boolean(effectivePeriod),
  });

  const rows = portfolioQuery.data?.rows.filter((r) => !status || r.status === status) ?? [];

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <h1 className="text-lg font-semibold text-slate-900">Client portfolio</h1>
      <p className="mt-1 text-sm text-slate-600">
        Every client, one period, at a glance — the ones that need a person are at the top.
      </p>

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <select
          value={effectivePeriod ?? ""}
          onChange={(e) => setPeriod(e.target.value)}
          aria-label="Period"
          className="rounded border border-slate-300 px-2 py-1.5 text-sm"
        >
          {(periodsQuery.data?.periods ?? []).map((p) => (
            <option key={p} value={p}>
              {formatPeriod(p)}
            </option>
          ))}
        </select>
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="Filter by status"
          className="rounded border border-slate-300 px-2 py-1.5 text-sm"
        >
          <option value="">All statuses</option>
          {STATUS_FILTERS.map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
            </option>
          ))}
        </select>
        {portfolioQuery.data && (
          <span className="ml-auto text-sm text-slate-500">
            {rows.length} of {portfolioQuery.data.total_clients} clients
          </span>
        )}
      </div>

      {portfolioQuery.data && <StatCards data={portfolioQuery.data} onPick={setStatus} active={status} />}

      {portfolioQuery.isLoading && <p className="mt-6 text-sm text-slate-500">Loading…</p>}
      {portfolioQuery.isError && (
        <p role="alert" className="mt-6 text-sm text-rose-700">
          {getErrorMessage(portfolioQuery.error)}
        </p>
      )}
      {portfolioQuery.data && rows.length === 0 && (
        <p className="mt-6 text-sm text-slate-500">No clients match this filter for this period.</p>
      )}

      <div className="mt-4 overflow-x-auto rounded-lg border border-slate-200 bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <th className="px-4 py-2">Client</th>
              <th className="px-4 py-2">GSTIN</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2 text-right">Open exceptions</th>
              <th className="px-4 py-2 text-right">ITC at risk</th>
              <th className="px-4 py-2">Nearest lapse</th>
              <th className="px-4 py-2">Assigned to</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <PortfolioTableRow key={r.gstin_id} row={r} period={effectivePeriod!}
                onOpen={() => open(r, effectivePeriod!, navigate)} />
            ))}
          </tbody>
        </table>
      </div>
    </main>
  );
}

function open(row: PortfolioRow, period: string, navigate: ReturnType<typeof useNavigate>) {
  if (row.run_version) {
    navigate(`/gstins/${row.gstin_id}/dashboard/${period}`);
  } else {
    navigate(`/gstins/${row.gstin_id}/upload`);
  }
}

function StatCards({
  data,
  onPick,
  active,
}: {
  data: { needs_review: number; awaiting_approval: number; needs_data: number; not_started: number; clean: number; total_itc_at_risk: string };
  onPick: (s: string) => void;
  active: string;
}) {
  const cards: { key: PortfolioStatus | ""; label: string; value: number | string }[] = [
    { key: "NEEDS_REVIEW", label: "Needs review", value: data.needs_review },
    { key: "AWAITING_APPROVAL", label: "Awaiting approval", value: data.awaiting_approval },
    { key: "NEEDS_DATA", label: "Not reconciled", value: data.needs_data },
    { key: "NOT_STARTED", label: "Nothing uploaded", value: data.not_started },
    { key: "CLEAN", label: "Clean", value: data.clean },
  ];
  return (
    <div className="mt-6 flex flex-wrap gap-3">
      {cards.map((c) => (
        <button
          key={c.key}
          onClick={() => onPick(active === c.key ? "" : c.key)}
          className={`min-w-32 flex-1 rounded-lg border p-3 text-left shadow-sm transition ${
            active === c.key ? "border-slate-900 ring-1 ring-slate-900" : "border-slate-200 bg-white"
          }`}
        >
          <div className="text-2xl font-semibold text-slate-900">{c.value}</div>
          <div className="text-xs text-slate-500">{c.label}</div>
        </button>
      ))}
      <div className="min-w-40 flex-1 rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
        <div className="text-2xl font-semibold text-rose-600">{formatINR(Number(data.total_itc_at_risk))}</div>
        <div className="text-xs text-slate-500">Total ITC at risk, firm-wide</div>
      </div>
    </div>
  );
}

function PortfolioTableRow({ row, onOpen }: { row: PortfolioRow; period: string; onOpen: () => void }) {
  const openCount = row.exceptions_open + row.exceptions_rework;
  return (
    <tr className="cursor-pointer border-b border-slate-100 hover:bg-slate-50" onClick={onOpen}>
      <td className="px-4 py-2.5">
        <span className="font-medium text-slate-900">{row.client_name}</span>
        <Link
          to={`/notices?client=${row.client_id}`}
          onClick={(e) => e.stopPropagation()}
          className="ml-2 text-xs text-slate-500 underline hover:text-slate-900"
        >
          notices
        </Link>
        <Link
          to={`/clients/${row.client_id}/audit-brief`}
          onClick={(e) => e.stopPropagation()}
          className="ml-2 text-xs text-slate-500 underline hover:text-slate-900"
        >
          brief
        </Link>
      </td>
      <td className="px-4 py-2.5 font-mono text-xs text-slate-600">{row.gstin}</td>
      <td className="px-4 py-2.5">
        <span className={`rounded border px-2 py-0.5 text-xs font-medium ${STATUS_CLASS[row.status]}`}>
          {STATUS_LABEL[row.status]}
        </span>
        {row.exceptions_pending_approval > 0 && row.status !== "AWAITING_APPROVAL" && (
          <span className="ml-1 text-xs text-slate-500">
            +{row.exceptions_pending_approval} pending approval
          </span>
        )}
      </td>
      <td className="px-4 py-2.5 text-right tabular-nums">{openCount || "—"}</td>
      <td className="px-4 py-2.5 text-right tabular-nums">
        {Number(row.itc_at_risk) > 0 ? formatINR(Number(row.itc_at_risk)) : "—"}
      </td>
      <td className="px-4 py-2.5 text-slate-600">
        {row.nearest_itc_lapse_deadline ? formatDate(row.nearest_itc_lapse_deadline) : "—"}
      </td>
      <td className="px-4 py-2.5 text-slate-600">{row.assigned_associate_name ?? "Unassigned"}</td>
    </tr>
  );
}
