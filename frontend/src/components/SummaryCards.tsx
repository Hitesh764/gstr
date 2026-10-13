import type { DashboardSummary } from "../types";
import { formatINR } from "../statusMeta";

interface Props {
  summary: DashboardSummary;
}

function Card({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: "danger" | "success" | "neutral";
}) {
  const valueClass =
    accent === "danger"
      ? "text-rose-600"
      : accent === "success"
        ? "text-emerald-600"
        : "text-slate-900";
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${valueClass}`}>{value}</div>
    </div>
  );
}

export default function SummaryCards({ summary }: Props) {
  return (
    <div className="flex flex-wrap gap-4">
      <div className="min-w-40 flex-1">
        <Card label="ITC claimed in books" value={formatINR(summary.total_itc_in_books)} />
      </div>
      <div className="min-w-40 flex-1">
        <Card
          label="ITC eligible (GSTR-2B)"
          value={formatINR(summary.total_itc_eligible_2b)}
          accent="success"
        />
      </div>
      <div className="min-w-40 flex-1">
        <Card label="ITC at risk" value={formatINR(summary.total_itc_at_risk)} accent="danger" />
      </div>
      {summary.total_itc_blocked_2b > 0 && (
        <div className="min-w-40 flex-1">
          <Card
            label="ITC blocked (Sec. 17(5))"
            value={formatINR(summary.total_itc_blocked_2b)}
            accent="danger"
          />
        </div>
      )}
      <div className="min-w-40 flex-1">
        <Card label="% auto-matched" value={`${summary.pct_auto_matched}%`} accent="success" />
      </div>
    </div>
  );
}
