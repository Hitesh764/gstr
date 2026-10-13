import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { getAuditBrief, getClient, getErrorMessage } from "../api/client";
import ItcRiskChart from "../components/audit/ItcRiskChart";
import VendorConcentrationBars from "../components/audit/VendorConcentrationBars";
import { formatPeriod, formatRupees } from "../noticeMeta";
import type { AuditBrief } from "../types";
import { formatDate } from "../utils/time";

const LOOKBACK_OPTIONS = [3, 6, 12, 24];

const SEVERITY = {
  critical: { label: "Critical", cls: "border-rose-200 bg-rose-50 text-rose-800", icon: "●" },
  warning: { label: "Warning", cls: "border-amber-200 bg-amber-50 text-amber-800", icon: "▲" },
} as const;

export default function AuditBriefPage() {
  const { clientId = "" } = useParams();
  const [lookback, setLookback] = useState(12);

  const clientQuery = useQuery({
    queryKey: ["client", clientId],
    queryFn: () => getClient(clientId),
    enabled: Boolean(clientId),
  });

  const briefQuery = useQuery({
    queryKey: ["audit-brief", clientId, lookback],
    queryFn: () => getAuditBrief(clientId, { lookback }),
    enabled: Boolean(clientId),
  });

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <Link to="/portfolio" className="text-sm text-slate-600 underline hover:text-slate-900">
        ← Portfolio
      </Link>

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <h1 className="text-lg font-semibold text-slate-900">
          Pre-audit brief{clientQuery.data ? `: ${clientQuery.data.name}` : ""}
        </h1>
        <select
          value={lookback}
          onChange={(e) => setLookback(Number(e.target.value))}
          aria-label="Lookback window"
          className="ml-auto rounded border border-slate-300 px-2 py-1.5 text-sm"
        >
          {LOOKBACK_OPTIONS.map((n) => (
            <option key={n} value={n}>
              Last {n} months
            </option>
          ))}
        </select>
      </div>
      <p className="mt-1 text-sm text-slate-600">
        What this client's GST reconciliation history says before a partner opens the file.
      </p>

      {briefQuery.isLoading && <p className="mt-6 text-sm text-slate-500">Loading…</p>}
      {briefQuery.isError && (
        <p role="alert" className="mt-6 text-sm text-rose-700">
          {getErrorMessage(briefQuery.error)}
        </p>
      )}

      {briefQuery.data && <BriefBody brief={briefQuery.data} />}
    </main>
  );
}

function BriefBody({ brief }: { brief: AuditBrief }) {
  return (
    <div className="mt-6 space-y-5">
      {brief.red_flags.length > 0 ? (
        <section className="space-y-2">
          {brief.red_flags.map((f, i) => {
            const s = SEVERITY[f.severity];
            return (
              <div key={i} className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-sm ${s.cls}`}>
                <span aria-hidden className="mt-0.5">{s.icon}</span>
                <span>
                  <span className="font-semibold">{s.label}.</span> {f.message}
                </span>
              </div>
            );
          })}
        </section>
      ) : (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">
          No red flags in this window: filing has been consistent, no vendor dominates,
          and no exception has gone stale.
        </div>
      )}

      <div className="flex flex-wrap gap-3">
        <StatTile
          label="ITC at risk"
          value={formatRupees(brief.latest_itc_at_risk)}
          sub={brief.latest_reconciled_period ? `as of ${formatPeriod(brief.latest_reconciled_period)}` : "no reconciled period yet"}
          tone={Number(brief.latest_itc_at_risk) > 0 ? "danger" : "good"}
        />
        <StatTile
          label="Filing consistency"
          value={`${brief.filing_consistency}%`}
          sub={`${brief.reconciled_periods} of ${brief.total_periods} months reconciled`}
          tone={brief.filing_consistency === 100 ? "good" : brief.filing_consistency < 50 ? "danger" : "warning"}
        />
        <StatTile
          label="Open exceptions"
          value={String(brief.open_exception_count)}
          sub={brief.stale_exceptions.length ? `${brief.stale_exceptions.length} over 60 days old` : "none stale"}
          tone={brief.stale_exceptions.length ? "warning" : "neutral"}
        />
      </div>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="text-sm font-semibold text-slate-900">ITC at risk by month</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Unsettled credit tied to a supplier-not-filed or value-mismatch exception. A dashed
          slot means the month was never reconciled, not that nothing was at risk.
        </p>
        <div className="mt-4">
          <ItcRiskChart points={brief.points} />
        </div>
      </section>

      <div className="grid gap-5 sm:grid-cols-2">
        <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <h2 className="text-sm font-semibold text-slate-900">Where ITC is concentrated</h2>
          <p className="mt-0.5 text-xs text-slate-500">Share of claimed ITC by vendor, this window.</p>
          <div className="mt-4">
            <VendorConcentrationBars vendors={brief.vendor_concentration} />
          </div>
        </section>

        <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <h2 className="text-sm font-semibold text-slate-900">Recurring problem vendors</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Flagged as an exception in {`>`}= 2 months of this window.
          </p>
          {brief.recurring_vendors.length === 0 ? (
            <p className="mt-3 text-sm text-slate-500">None. Every vendor's issues, if any, were one-offs.</p>
          ) : (
            <ul className="mt-3 space-y-2 text-sm">
              {brief.recurring_vendors.map((v) => (
                <li key={v.vendor_gstin} className="border-b border-slate-100 pb-2 last:border-0">
                  <div className="font-medium text-slate-800">{v.vendor_name || v.vendor_gstin}</div>
                  <div className="text-xs text-slate-500">
                    {v.periods_flagged.map(formatPeriod).join(", ")}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="text-sm font-semibold text-slate-900">Exceptions open more than 60 days</h2>
        {brief.stale_exceptions.length === 0 ? (
          <p className="mt-3 text-sm text-slate-500">None outstanding.</p>
        ) : (
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500">
                  <th className="py-1.5 pr-4">Period</th>
                  <th className="py-1.5 pr-4">Vendor</th>
                  <th className="py-1.5 pr-4">Invoice</th>
                  <th className="py-1.5 pr-4">Status</th>
                  <th className="py-1.5 text-right">Age</th>
                </tr>
              </thead>
              <tbody>
                {brief.stale_exceptions.map((e) => (
                  <tr key={e.exception_id} className="border-b border-slate-100 last:border-0">
                    <td className="py-1.5 pr-4">{formatPeriod(e.period)}</td>
                    <td className="py-1.5 pr-4">{e.vendor_name ?? "—"}</td>
                    <td className="py-1.5 pr-4 font-mono text-xs">{e.invoice_number ?? "—"}</td>
                    <td className="py-1.5 pr-4">{e.status.replace(/_/g, " ").toLowerCase()}</td>
                    <td className="py-1.5 text-right tabular-nums">{e.age_days} days</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <p className="text-xs text-slate-500">
        {brief.scope_note} Generated {formatDate(brief.generated_at)}.
      </p>
    </div>
  );
}

function StatTile({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub: string;
  tone: "good" | "warning" | "danger" | "neutral";
}) {
  const toneClass = {
    good: "text-emerald-700",
    warning: "text-amber-700",
    danger: "text-rose-700",
    neutral: "text-slate-900",
  }[tone];
  return (
    <div className="min-w-40 flex-1 rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
      <div className={`text-2xl font-semibold ${toneClass}`}>{value}</div>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-0.5 text-xs text-slate-400">{sub}</div>
    </div>
  );
}
