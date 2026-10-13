import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getErrorMessage, listExceptionQueue } from "../api/client";
import type { ExceptionListItem, Urgency } from "../types";

const URGENCY_STYLES: Record<Urgency, string> = {
  lapsed: "border-rose-300 bg-rose-50 text-rose-800",
  critical: "border-rose-200 bg-rose-50 text-rose-700",
  warning: "border-amber-200 bg-amber-50 text-amber-800",
  ok: "border-emerald-200 bg-emerald-50 text-emerald-700",
  none: "border-slate-200 bg-slate-50 text-slate-600",
};

const URGENCY_LABELS: Record<Urgency, string> = {
  lapsed: "Lapsed",
  critical: "Under 30 days",
  warning: "Under 90 days",
  ok: "In time",
  none: "No deadline",
};

const STATUS_OPTIONS = [
  "OPEN",
  "IN_PROGRESS",
  "PENDING_APPROVAL",
  "APPROVED",
  "REWORK",
  "AUTO_RESOLVED",
];

export default function ExceptionQueuePage() {
  const [status, setStatus] = useState<string>("");
  const [urgency, setUrgency] = useState<string>("");

  const queueQuery = useQuery({
    queryKey: ["exception-queue", status, urgency],
    queryFn: () =>
      listExceptionQueue({
        status: status || undefined,
        urgency: urgency || undefined,
      }),
  });

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <h1 className="text-lg font-semibold text-slate-900">Exception queue</h1>
      <p className="mt-1 text-sm text-slate-600">
        Every unresolved item across all clients, with the credit closest to lapsing
        shown first.
      </p>

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          className="rounded border border-slate-300 px-2 py-1.5 text-sm"
        >
          <option value="">All workflow states</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s.replace("_", " ").toLowerCase()}
            </option>
          ))}
        </select>
        <select
          value={urgency}
          onChange={(e) => setUrgency(e.target.value)}
          className="rounded border border-slate-300 px-2 py-1.5 text-sm"
        >
          <option value="">All urgencies</option>
          <option value="lapsed">Lapsed</option>
          <option value="critical">Under 30 days</option>
          <option value="warning">Under 90 days</option>
          <option value="ok">In time</option>
        </select>
        {queueQuery.data && (
          <span className="ml-auto text-sm text-slate-500">
            {queueQuery.data.length} items
          </span>
        )}
      </div>

      {queueQuery.isLoading && <p className="mt-6 text-sm text-slate-500">Loading…</p>}
      {queueQuery.isError && (
        <p role="alert" className="mt-6 text-sm text-rose-700">
          {getErrorMessage(queueQuery.error)}
        </p>
      )}
      {queueQuery.data?.length === 0 && (
        <p className="mt-6 text-sm text-slate-500">Nothing outstanding. </p>
      )}

      <div className="mt-4 space-y-2">
        {queueQuery.data?.map((item) => (
          <QueueRow key={item.id} item={item} />
        ))}
      </div>
    </main>
  );
}

function QueueRow({ item }: { item: ExceptionListItem }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-sm">
      <span className="font-mono text-xs text-slate-500">{item.period}</span>
      <span className="text-slate-800">{item.status.replace("_", " ").toLowerCase()}</span>
      {item.resolution_notes && (
        <span className="max-w-md truncate text-slate-500">{item.resolution_notes}</span>
      )}
      <span
        className={`ml-auto rounded border px-2 py-0.5 text-xs font-medium ${URGENCY_STYLES[item.urgency]}`}
      >
        {URGENCY_LABELS[item.urgency]}
        {item.days_until_lapse !== null && item.days_until_lapse !== undefined
          ? ` · ${item.days_until_lapse} days`
          : ""}
      </span>
    </div>
  );
}
