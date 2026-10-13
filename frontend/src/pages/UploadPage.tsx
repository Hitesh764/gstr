import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchGstr2bLive,
  getErrorMessage,
  getGspStatus,
  listUploads,
  loadSampleGstr2b,
  runReconciliation,
  uploadGstr2b,
  uploadPurchaseRegister,
} from "../api/client";
import FileUpload from "../components/FileUpload";
import type { FetchResult, GspFetchResult, RowError, UploadResult } from "../types";
import { STATE_CLASS, STATE_LABEL } from "../gspMeta";
import { formatDateTime } from "../utils/time";

function defaultPeriod(): string {
  const now = new Date();
  const month = now.getMonth() === 0 ? 12 : now.getMonth();
  const year = now.getMonth() === 0 ? now.getFullYear() - 1 : now.getFullYear();
  return `${year}-${String(month).padStart(2, "0")}`;
}

export default function UploadPage() {
  const { gstinId } = useParams<{ gstinId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [period, setPeriod] = useState(defaultPeriod());
  const [prResult, setPrResult] = useState<UploadResult | null>(null);
  const [gstr2bResult, setGstr2bResult] = useState<FetchResult | null>(null);
  const [liveResult, setLiveResult] = useState<GspFetchResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const uploadsQuery = useQuery({
    queryKey: ["uploads", gstinId, period],
    queryFn: () => listUploads(gstinId!, period),
    enabled: Boolean(gstinId),
  });

  // Live access is optional: a firm or client that never connects still has
  // the file upload below, which is a permanent path and not a stopgap.
  const gspQuery = useQuery({
    queryKey: ["gsp", gstinId],
    queryFn: () => getGspStatus(gstinId!),
    enabled: Boolean(gstinId),
    retry: false,
  });
  const gspState = gspQuery.data?.state ?? "NONE";
  const gspConnected = gspState === "ACTIVE" || gspState === "EXPIRING";

  const fetchLive = useMutation({
    mutationFn: () => fetchGstr2bLive(gstinId!, period),
    onSuccess: (res) => {
      setLiveResult(res);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["gsp", gstinId] });
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  const uploadPr = useMutation({
    mutationFn: ({ file, force }: { file: File; force: boolean }) =>
      uploadPurchaseRegister(gstinId!, period, file, force),
    onSuccess: (res) => {
      setPrResult(res);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["uploads", gstinId, period] });
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  const uploadTwoB = useMutation({
    mutationFn: (file: File) => uploadGstr2b(gstinId!, period, file),
    onSuccess: (res) => {
      setGstr2bResult(res);
      setError(null);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  const loadSample = useMutation({
    mutationFn: () => loadSampleGstr2b(gstinId!, period),
    onSuccess: (res) => {
      setGstr2bResult(res);
      setError(null);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  const reconcile = useMutation({
    mutationFn: () => runReconciliation(gstinId!, period),
    onSuccess: () => navigate(`/gstins/${gstinId}/dashboard/${period}`),
    onError: (err) => setError(getErrorMessage(err)),
  });

  return (
    <main className="mx-auto max-w-4xl px-4 py-8">
      <h1 className="text-lg font-semibold text-slate-900">Upload data</h1>
      <p className="mt-1 text-sm text-slate-600">
        Every upload is kept as a new version. Nothing you have uploaded before is
        overwritten.
      </p>

      {error && (
        <div
          role="alert"
          className="mt-4 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
        >
          {error}
        </div>
      )}

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <label htmlFor="period" className="block text-sm font-medium text-slate-700">
          Return period
        </label>
        <input
          id="period"
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
          placeholder="YYYY-MM"
          className="mt-1 w-40 rounded border border-slate-300 px-3 py-2 font-mono text-sm"
        />
      </section>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="text-sm font-semibold text-slate-900">1. Purchase register</h2>
        <p className="mt-1 text-xs text-slate-500">
          CSV or XLSX with columns: vendor_name, vendor_gstin, invoice_number,
          invoice_date, taxable_value, cgst, sgst, igst, total_value.
        </p>
        <div className="mt-3">
          <FileUpload
            label={uploadPr.isPending ? "Uploading…" : "Drop the purchase register here, or click to choose"}
            hint="CSV or XLSX"
            accept=".csv,.xlsx,.xls"
            disabled={uploadPr.isPending}
            onFile={(file) => uploadPr.mutate({ file, force: false })}
          />
        </div>

        {prResult && <UploadFeedback result={prResult} />}
      </section>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="text-sm font-semibold text-slate-900">2. GSTR-2B</h2>
        <p className="mt-1 text-xs text-slate-500">
          Fetch it straight from the GST portal, or upload the GSTR-2B JSON. Uploading
          always works, whether or not live access is connected.
        </p>

        <div className="mt-3 rounded border border-slate-200 bg-slate-50 px-3 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-slate-900">Fetch from the GST portal</span>
            <span
              className={`rounded border px-2 py-0.5 text-xs font-medium ${STATE_CLASS[gspState]}`}
            >
              {STATE_LABEL[gspState]}
            </span>
          </div>
          {gspConnected ? (
            <>
              <p className="mt-1 text-xs text-slate-600">
                Pulls GSTR-2B for {period} directly, then reconciles it against the latest
                purchase register if one is loaded.
              </p>
              <button
                onClick={() => fetchLive.mutate()}
                disabled={fetchLive.isPending}
                className="mt-2 rounded bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
              >
                {fetchLive.isPending ? "Fetching…" : "Fetch GSTR-2B now"}
              </button>
            </>
          ) : (
            <p className="mt-1 text-xs text-slate-600">
              {gspState === "EXPIRED"
                ? "Access to the GST portal has expired for this GSTIN. "
                : "This GSTIN is not connected to the GST portal. "}
              <Link to={`/?connect=${gstinId}`} className="font-medium text-slate-900 underline">
                {gspState === "EXPIRED" ? "Reconnect" : "Connect live GST access"}
              </Link>{" "}
              to fetch automatically, or use a file below.
            </p>
          )}
          {liveResult && <LiveFetchFeedback result={liveResult} gstinId={gstinId!} period={period} />}
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-3">
          <div className="min-w-72 flex-1">
            <FileUpload
              label={uploadTwoB.isPending ? "Uploading…" : "Drop the GSTR-2B JSON here"}
              hint="JSON"
              accept=".json"
              disabled={uploadTwoB.isPending}
              onFile={(file) => uploadTwoB.mutate(file)}
            />
          </div>
          <button
            onClick={() => loadSample.mutate()}
            disabled={loadSample.isPending}
            className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            {loadSample.isPending ? "Loading…" : "Load sample GSTR-2B"}
          </button>
        </div>
        {gstr2bResult && <Gstr2bFeedback result={gstr2bResult} />}
      </section>

      <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <h2 className="text-sm font-semibold text-slate-900">3. Reconcile</h2>
        <button
          onClick={() => reconcile.mutate()}
          disabled={reconcile.isPending}
          className="mt-3 rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {reconcile.isPending ? "Running…" : "Run reconciliation"}
        </button>
      </section>

      {uploadsQuery.data && uploadsQuery.data.length > 0 && (
        <section className="mt-6 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <h2 className="text-sm font-semibold text-slate-900">Upload history</h2>
          <table className="mt-3 w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
                <th className="py-2">Version</th>
                <th>File</th>
                <th>Rows</th>
                <th>Uploaded</th>
                <th>Current</th>
              </tr>
            </thead>
            <tbody>
              {uploadsQuery.data.map((u) => (
                <tr key={u.id} className="border-b border-slate-100">
                  <td className="py-2">v{u.version}</td>
                  <td className="text-slate-600">{u.original_filename ?? "—"}</td>
                  <td>{u.row_count}</td>
                  <td className="text-slate-500">
                    {formatDateTime(u.uploaded_at)}
                  </td>
                  <td>{u.is_current ? "Yes" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </main>
  );
}

function LiveFetchFeedback({
  result,
  gstinId,
  period,
}: {
  result: GspFetchResult;
  gstinId: string;
  period: string;
}) {
  const ok = result.status === "SUCCESS";
  return (
    <div
      className={`mt-3 rounded border px-3 py-2 text-sm ${
        ok
          ? "border-emerald-200 bg-emerald-50 text-emerald-900"
          : "border-rose-200 bg-rose-50 text-rose-900"
      }`}
    >
      <p className="font-medium">{ok ? "GSTR-2B fetched." : "Could not fetch GSTR-2B."}</p>
      <p className="mt-0.5">{result.message}</p>
      {result.warnings.length > 0 && (
        <ul className="mt-1 list-disc pl-5 text-xs">
          {result.warnings.slice(0, 5).map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
      {ok && result.reconciled && (
        <Link
          to={`/gstins/${gstinId}/dashboard/${period}`}
          className="mt-2 inline-block font-medium underline"
        >
          View the reconciliation
        </Link>
      )}
    </div>
  );
}

function Gstr2bFeedback({ result }: { result: FetchResult }) {
  // A parse that yields no invoices returns HTTP 200 with inserted: 0. That
  // is still a failed upload from the user's point of view, so it must not be
  // reported in the same green text as a successful one.
  if (result.inserted === 0) {
    return (
      <div className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
        <p className="font-medium">No invoices were loaded from that file.</p>
        {result.errors.length > 0 ? (
          <ul className="mt-1 space-y-1">
            {result.errors.slice(0, 10).map((e, i) => (
              <li key={i}>{e.message}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-1">
            The file parsed, but its <code>invoices</code> array was empty. Check
            that it is a GSTR-2B statement for this period.
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="mt-3">
      <p className="text-sm text-emerald-700">
        Loaded {result.inserted} GSTR-2B invoices
        {result.fetch ? ` as version ${result.fetch.version}` : ""}.
      </p>
      {result.errors.length > 0 && (
        <div className="mt-2 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          <p className="font-medium">{result.errors.length} invoice(s) were skipped</p>
          <ul className="mt-1 space-y-1">
            {result.errors.slice(0, 10).map((e, i) => (
              <li key={i}>{e.message}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function UploadFeedback({ result }: { result: UploadResult }) {
  if (result.accepted) {
    return (
      <div className="mt-3">
        <p className="text-sm text-emerald-700">
          Imported {result.inserted} rows as version {result.upload?.version}.
        </p>
        {result.warnings.length > 0 && <IssueList title="Warnings" issues={result.warnings} tone="amber" />}
      </div>
    );
  }

  return (
    <div className="mt-3">
      <p className="text-sm font-medium text-rose-700">
        Nothing was imported. Fix these {result.errors.length} problem(s) and upload again.
      </p>
      <IssueList title="Errors" issues={result.errors} tone="rose" />
      {result.warnings.length > 0 && <IssueList title="Warnings" issues={result.warnings} tone="amber" />}
    </div>
  );
}

function IssueList({
  title,
  issues,
  tone,
}: {
  title: string;
  issues: RowError[];
  tone: "rose" | "amber";
}) {
  const colours =
    tone === "rose"
      ? "border-rose-200 bg-rose-50 text-rose-800"
      : "border-amber-200 bg-amber-50 text-amber-800";
  return (
    <div className={`mt-2 rounded border px-3 py-2 text-sm ${colours}`}>
      <p className="font-medium">{title}</p>
      <ul className="mt-1 space-y-1">
        {issues.slice(0, 25).map((issue, i) => (
          <li key={i}>
            {issue.row ? <span className="font-mono text-xs">Row {issue.row}</span> : null}
            {issue.field ? <span className="font-mono text-xs"> [{issue.field}]</span> : null}{" "}
            {issue.message}
          </li>
        ))}
        {issues.length > 25 && <li className="text-xs">…and {issues.length - 25} more.</li>}
      </ul>
    </div>
  );
}
