import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getErrorMessage,
  getNoticeStats,
  listClients,
  listNotices,
  uploadNotice,
} from "../api/client";
import {
  STATUS_CLASS,
  STATUS_LABEL,
  URGENCY_CLASS,
  deadlineText,
  formatRupees,
  periodRange,
} from "../noticeMeta";
import type { Client, NoticeStats, NoticeSummary } from "../types";
import { formatDate } from "../utils/time";

const STATUS_FILTERS = [
  "RECEIVED",
  "UNDER_REVIEW",
  "RESPONSE_DRAFTED",
  "PARTNER_APPROVED",
  "FILED",
  "CLOSED",
] as const;

export default function NoticesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const clientId = searchParams.get("client") ?? undefined;

  const [status, setStatus] = useState("");
  const [mine, setMine] = useState(false);
  const [includeFinished, setIncludeFinished] = useState(false);
  const [showUpload, setShowUpload] = useState(false);

  const clientsQuery = useQuery({ queryKey: ["clients"], queryFn: listClients });
  const clientName = clientsQuery.data?.find((c) => c.id === clientId)?.name;

  // Firm-wide, the page is the list of work still to do. For one client it is
  // that client's whole history, finished notices included.
  const openOnly = !clientId && !includeFinished && !status;

  const noticesQuery = useQuery({
    queryKey: ["notices", { clientId, status, mine, openOnly }],
    queryFn: () =>
      listNotices({
        clientId,
        status: status || undefined,
        openOnly,
        assignedTo: mine ? "me" : undefined,
      }),
  });

  const statsQuery = useQuery({
    queryKey: ["notice-stats"],
    queryFn: getNoticeStats,
    enabled: !clientId,
  });

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-lg font-semibold text-slate-900">
          {clientId ? `Notices: ${clientName ?? "client"}` : "Notices"}
        </h1>
        {clientId && (
          <button
            onClick={() => setSearchParams({})}
            className="text-sm text-slate-600 underline hover:text-slate-900"
          >
            Show all clients
          </button>
        )}
        <button
          onClick={() => setShowUpload((v) => !v)}
          className="ml-auto rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800"
        >
          {showUpload ? "Close" : "Upload a notice"}
        </button>
      </div>
      <p className="mt-1 text-sm text-slate-600">
        {clientId
          ? "Every notice for this client, open and closed."
          : "Open notices across all clients, with the nearest deadline first."}
      </p>

      {showUpload && (
        <UploadPanel clients={clientsQuery.data ?? []} defaultClientId={clientId} />
      )}

      {statsQuery.data && <StatCards stats={statsQuery.data} />}

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="Filter by status"
          className="rounded border border-slate-300 px-2 py-1.5 text-sm"
        >
          <option value="">{clientId ? "All statuses" : "All open statuses"}</option>
          {STATUS_FILTERS.map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" checked={mine} onChange={(e) => setMine(e.target.checked)} />
          Assigned to me
        </label>
        {!clientId && (
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={includeFinished}
              onChange={(e) => setIncludeFinished(e.target.checked)}
            />
            Include filed and closed
          </label>
        )}
        {noticesQuery.data && (
          <span className="ml-auto text-sm text-slate-500">{noticesQuery.data.length} notices</span>
        )}
      </div>

      {noticesQuery.isLoading && <p className="mt-6 text-sm text-slate-500">Loading…</p>}
      {noticesQuery.isError && (
        <p role="alert" className="mt-6 text-sm text-rose-700">
          {getErrorMessage(noticesQuery.error)}
        </p>
      )}
      {noticesQuery.data?.length === 0 && (
        <p className="mt-6 text-sm text-slate-500">
          {clientId ? "No notices for this client yet." : "No open notices. "}
        </p>
      )}

      <div className="mt-4 space-y-2">
        {noticesQuery.data?.map((n) => <NoticeRow key={n.id} notice={n} />)}
      </div>
    </main>
  );
}

function StatCards({ stats }: { stats: NoticeStats }) {
  const cards: { label: string; value: number; tone: string }[] = [
    { label: "Open", value: stats.open, tone: "text-slate-900" },
    { label: "Overdue", value: stats.overdue, tone: stats.overdue ? "text-rose-700" : "text-slate-900" },
    {
      label: "Due within 7 days",
      value: stats.due_within_7_days,
      tone: stats.due_within_7_days ? "text-amber-700" : "text-slate-900",
    },
    { label: "Awaiting approval", value: stats.awaiting_approval, tone: "text-slate-900" },
    { label: "Unassigned", value: stats.unassigned, tone: "text-slate-900" },
  ];
  return (
    <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-5">
      {cards.map((c) => (
        <div key={c.label} className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
          <div className={`text-2xl font-semibold ${c.tone}`}>{c.value}</div>
          <div className="text-xs text-slate-500">{c.label}</div>
        </div>
      ))}
    </div>
  );
}

function NoticeRow({ notice: n }: { notice: NoticeSummary }) {
  const flags: string[] = [];
  if (n.extraction_status === "NEEDS_OCR" || n.extraction_status === "FAILED") flags.push("Needs text");
  else if (n.analysis_status === "FAILED") flags.push("Not read");
  if (n.response_deadline && !n.deadline_confirmed && n.status !== "FILED" && n.status !== "CLOSED") {
    flags.push("Deadline unconfirmed");
  }

  return (
    <Link
      to={`/notices/${n.id}`}
      className="block rounded-lg border border-slate-200 bg-white p-3 text-sm shadow-sm hover:border-slate-300"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="font-medium text-slate-900">{n.title || n.original_filename || "Notice"}</span>
        {n.notice_reference && (
          <span className="font-mono text-xs text-slate-500">{n.notice_reference}</span>
        )}
        <span className={`rounded border px-2 py-0.5 text-xs font-medium ${STATUS_CLASS[n.status]}`}>
          {STATUS_LABEL[n.status]}
        </span>
        <span
          className={`ml-auto rounded border px-2 py-0.5 text-xs font-medium ${URGENCY_CLASS[n.urgency]}`}
          title={n.response_deadline ? `Due ${formatDate(n.response_deadline)}` : undefined}
        >
          {n.response_deadline ? `${formatDate(n.response_deadline)} · ` : ""}
          {n.status === "FILED" || n.status === "CLOSED" ? "Finished" : deadlineText(n)}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
        <span>{n.client_name}</span>
        {n.gstin && <span className="font-mono">{n.gstin}</span>}
        {n.notice_type_label && <span>{n.notice_type_label}</span>}
        <span>Period {periodRange(n)}</span>
        {n.amount_in_question && <span>{formatRupees(n.amount_in_question)} in question</span>}
        <span>{n.assigned_to_name ? `Assigned to ${n.assigned_to_name}` : "Unassigned"}</span>
        {flags.map((f) => (
          <span key={f} className="font-medium text-amber-700">
            {f}
          </span>
        ))}
      </div>
    </Link>
  );
}

function UploadPanel({
  clients,
  defaultClientId,
}: {
  clients: Client[];
  defaultClientId?: string;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [clientId, setClientId] = useState(defaultClientId ?? "");
  const [gstinId, setGstinId] = useState("");
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);

  const client = clients.find((c) => c.id === clientId);

  const upload = useMutation({
    mutationFn: () =>
      uploadNotice({
        file: file as File,
        clientId,
        clientGstinId: gstinId || undefined,
        title: title.trim() || undefined,
      }),
    onSuccess: (notice) => {
      void queryClient.invalidateQueries({ queryKey: ["notices"] });
      void queryClient.invalidateQueries({ queryKey: ["notice-stats"] });
      navigate(`/notices/${notice.id}`);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!clientId) return setError("Choose the client this notice is for.");
    if (!file) return setError("Choose the notice file to upload.");
    upload.mutate();
  }

  return (
    <form
      onSubmit={onSubmit}
      className="mt-6 rounded-lg border border-slate-200 bg-white p-4 shadow-sm"
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="n-client" className="block text-xs font-medium text-slate-700">
            Client
          </label>
          <select
            id="n-client"
            value={clientId}
            onChange={(e) => {
              setClientId(e.target.value);
              setGstinId("");
            }}
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
          >
            <option value="">Choose a client…</option>
            {clients.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="n-gstin" className="block text-xs font-medium text-slate-700">
            GSTIN
          </label>
          <select
            id="n-gstin"
            value={gstinId}
            onChange={(e) => setGstinId(e.target.value)}
            disabled={!client || client.gstins.length < 2}
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 font-mono text-sm disabled:bg-slate-50"
          >
            <option value="">
              {client && client.gstins.length === 1 ? client.gstins[0].gstin : "Read from the notice"}
            </option>
            {client && client.gstins.length > 1
              ? client.gstins.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.gstin}
                  </option>
                ))
              : null}
          </select>
        </div>
        <div>
          <label htmlFor="n-title" className="block text-xs font-medium text-slate-700">
            Title (optional)
          </label>
          <input
            id="n-title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={300}
            placeholder="Filled in from the notice if left blank"
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
          />
        </div>
        <div>
          <label htmlFor="n-file" className="block text-xs font-medium text-slate-700">
            Notice (PDF, PNG or JPEG)
          </label>
          <input
            id="n-file"
            type="file"
            accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="mt-1 block w-full text-sm"
          />
        </div>
      </div>

      {error && (
        <p role="alert" className="mt-3 text-sm text-rose-700">
          {error}
        </p>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={upload.isPending}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {upload.isPending ? "Uploading and reading…" : "Upload and read"}
        </button>
        <span className="text-xs text-slate-500">
          The file is encrypted before it is stored. The details it contains are read
          automatically, and you check them afterwards.
        </span>
      </div>
    </form>
  );
}
