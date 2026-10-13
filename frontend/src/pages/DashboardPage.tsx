import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  approveException,
  downloadExport,
  getErrorMessage,
  getExceptions,
  getSummary,
  resolveException,
  runReconciliation,
} from "../api/client";
import SummaryCards from "../components/SummaryCards";
import ExceptionTable from "../components/ExceptionTable";

export default function DashboardPage() {
  const { gstinId, period } = useParams<{ gstinId: string; period: string }>();
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);

  const summaryQuery = useQuery({
    queryKey: ["summary", gstinId, period],
    queryFn: () => getSummary(gstinId!, period!),
    enabled: Boolean(gstinId && period),
  });

  const exceptionsQuery = useQuery({
    queryKey: ["exceptions", gstinId, period],
    queryFn: () => getExceptions(gstinId!, period!),
    enabled: Boolean(gstinId && period),
  });

  function invalidate() {
    void queryClient.invalidateQueries({ queryKey: ["summary", gstinId, period] });
    void queryClient.invalidateQueries({ queryKey: ["exceptions", gstinId, period] });
  }

  const rerun = useMutation({
    mutationFn: () => runReconciliation(gstinId!, period!),
    onSuccess: invalidate,
    onError: (err) => setActionError(getErrorMessage(err)),
  });

  const download = useMutation({
    mutationFn: () => downloadExport(gstinId!, period!),
    onError: (err) => setActionError(getErrorMessage(err)),
  });

  async function handleResolve(exceptionId: string, note: string) {
    setActionError(null);
    try {
      await resolveException(exceptionId, note);
      invalidate();
    } catch (err) {
      const message = getErrorMessage(err);
      setActionError(message);
      throw new Error(message);
    }
  }

  async function handleApprove(
    exceptionId: string,
    approved: boolean,
    reworkReason?: string,
  ) {
    setActionError(null);
    try {
      await approveException(exceptionId, approved, reworkReason);
      invalidate();
    } catch (err) {
      const message = getErrorMessage(err);
      setActionError(message);
      throw new Error(message);
    }
  }

  const loading = summaryQuery.isLoading || exceptionsQuery.isLoading;
  const loadError = summaryQuery.error ?? exceptionsQuery.error;

  return (
    <main className="mx-auto max-w-6xl px-4 py-8">
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <h1 className="text-lg font-semibold text-slate-900">
            Reconciliation · {period}
          </h1>
          <Link to="/" className="text-sm text-slate-600 underline">
            Back to clients
          </Link>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <button
            onClick={() => rerun.mutate()}
            disabled={rerun.isPending}
            className="rounded border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            {rerun.isPending ? "Re-running…" : "Re-run reconciliation"}
          </button>
          <button
            onClick={() => download.mutate()}
            disabled={download.isPending}
            className="rounded bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {download.isPending ? "Preparing…" : "Export exceptions (XLSX)"}
          </button>
        </div>
      </div>

      {actionError && (
        <div
          role="alert"
          className="mt-4 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700"
        >
          {actionError}
        </div>
      )}

      {loadError && (
        <div
          role="alert"
          className="mt-4 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800"
        >
          {getErrorMessage(loadError)}
        </div>
      )}

      {loading && <p className="mt-6 text-sm text-slate-500">Loading…</p>}

      {summaryQuery.data && (
        <>
          <div className="mt-6">
            <SummaryCards summary={summaryQuery.data} />
          </div>
          <p className="mt-3 text-sm text-slate-600">
            {summaryQuery.data.exceptions_open} open,{" "}
            {summaryQuery.data.exceptions_pending_approval} awaiting approval, out of{" "}
            {summaryQuery.data.total_entries} reconciled lines.
          </p>
        </>
      )}

      {exceptionsQuery.data && (
        <div className="mt-6">
          <ExceptionTable
            results={exceptionsQuery.data}
            onResolve={handleResolve}
            onApprove={handleApprove}
          />
        </div>
      )}
    </main>
  );
}
