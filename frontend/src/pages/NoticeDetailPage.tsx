import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import axios from "axios";
import {
  deleteNotice,
  downloadNoticeFile,
  getClient,
  getErrorMessage,
  getNotice,
  getNoticeTypes,
  listFirmUsers,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import NoticeActionBar from "../components/notices/NoticeActionBar";
import NoticeDetailsCard from "../components/notices/NoticeDetailsCard";
import NoticeDraftPanel from "../components/notices/NoticeDraftPanel";
import NoticeHistory from "../components/notices/NoticeHistory";
import NoticePipeline from "../components/notices/NoticePipeline";
import NoticeReadingPanel from "../components/notices/NoticeReadingPanel";
import {
  STATUS_CLASS,
  STATUS_LABEL,
  URGENCY_CLASS,
  deadlineText,
  formatRupees,
} from "../noticeMeta";
import type { NoticeDetail } from "../types";
import { formatDate, formatDateTime } from "../utils/time";

export default function NoticeDetailPage() {
  const { noticeId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { hasRole } = useAuth();
  const [actionError, setActionError] = useState<string | null>(null);

  const noticeQuery = useQuery({
    queryKey: ["notice", noticeId],
    queryFn: () => getNotice(noticeId),
    enabled: Boolean(noticeId),
    retry: (count, err) => !(axios.isAxiosError(err) && err.response?.status === 404) && count < 2,
  });
  const notice = noticeQuery.data;

  const typesQuery = useQuery({ queryKey: ["notice-types"], queryFn: getNoticeTypes, staleTime: Infinity });
  const clientQuery = useQuery({
    queryKey: ["client", notice?.client_id],
    queryFn: () => getClient(notice!.client_id),
    enabled: Boolean(notice?.client_id),
  });
  const staffQuery = useQuery({ queryKey: ["firm-users"], queryFn: listFirmUsers });

  // Anything that changes a notice also changes the lists, the counters on the
  // notices page and possibly other people's notifications.
  function onChanged(updated: NoticeDetail) {
    queryClient.setQueryData(["notice", noticeId], updated);
    void queryClient.invalidateQueries({ queryKey: ["notices"] });
    void queryClient.invalidateQueries({ queryKey: ["notice-stats"] });
    void queryClient.invalidateQueries({ queryKey: ["notifications"] });
    void queryClient.invalidateQueries({ queryKey: ["notice-evidence", noticeId] });
  }

  const remove = useMutation({
    mutationFn: () => deleteNotice(noticeId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["notices"] });
      void queryClient.invalidateQueries({ queryKey: ["notice-stats"] });
      navigate("/notices");
    },
    onError: (err) => setActionError(getErrorMessage(err)),
  });

  async function onDownload() {
    if (!notice) return;
    setActionError(null);
    try {
      await downloadNoticeFile(notice.id, notice.original_filename ?? "notice");
    } catch (err) {
      setActionError(getErrorMessage(err));
    }
  }

  if (noticeQuery.isLoading) {
    return <main className="mx-auto max-w-5xl px-4 py-8 text-sm text-slate-500">Loading…</main>;
  }
  if (noticeQuery.isError || !notice) {
    const notFound = axios.isAxiosError(noticeQuery.error) && noticeQuery.error.response?.status === 404;
    return (
      <main className="mx-auto max-w-5xl px-4 py-8">
        <p role="alert" className="text-sm text-rose-700">
          {notFound ? "This notice does not exist, or you do not have access to it." : getErrorMessage(noticeQuery.error)}
        </p>
        <Link to="/notices" className="mt-3 inline-block text-sm underline">
          Back to notices
        </Link>
      </main>
    );
  }

  const staff = (staffQuery.data ?? []).filter((u) => u.is_active && u.role !== "CLIENT_VIEWER");
  const finished = notice.status === "FILED" || notice.status === "CLOSED";

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <Link to="/notices" className="text-sm text-slate-600 underline hover:text-slate-900">
        ← All notices
      </Link>

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <h1 className="text-lg font-semibold text-slate-900">
          {notice.title || notice.original_filename || "Notice"}
        </h1>
        <span className={`rounded border px-2 py-0.5 text-xs font-medium ${STATUS_CLASS[notice.status]}`}>
          {STATUS_LABEL[notice.status]}
        </span>
        <span
          className={`rounded border px-2 py-0.5 text-xs font-medium ${URGENCY_CLASS[notice.urgency]}`}
        >
          {finished ? "Finished" : `${notice.response_deadline ? formatDate(notice.response_deadline) + " · " : ""}${deadlineText(notice)}`}
        </span>
      </div>
      <p className="mt-1 text-sm text-slate-600">
        <Link to={`/notices?client=${notice.client_id}`} className="underline hover:text-slate-900">
          {notice.client_name}
        </Link>
        {notice.gstin && <span className="ml-2 font-mono text-xs">{notice.gstin}</span>}
        {notice.amount_in_question && <span className="ml-3">{formatRupees(notice.amount_in_question)} in question</span>}
      </p>

      <div className="mt-5">
        <NoticePipeline status={notice.status} />
      </div>

      <section className="mt-5 rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
        <NoticeActionBar notice={notice} onChanged={onChanged} />
        {finished && (
          <dl className="mt-3 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            {notice.approved_by_name && (
              <Row label="Approved by" value={`${notice.approved_by_name}, ${formatDateTime(notice.approved_at)}`} />
            )}
            {notice.filed_at && (
              <Row
                label="Filed"
                value={`${formatDate(notice.filed_at)}${notice.filed_by_name ? ` by ${notice.filed_by_name}` : ""}`}
              />
            )}
            {notice.filing_reference && <Row label="Reference" value={notice.filing_reference} mono />}
            {notice.outcome && <Row label="Outcome" value={notice.outcome} />}
          </dl>
        )}
        {!finished && (notice.drafted_by_name || notice.approved_by_name) && (
          <p className="mt-3 text-xs text-slate-500">
            {notice.drafted_by_name && <>Response last written by {notice.drafted_by_name}. </>}
            {notice.submitted_by_name && <>Submitted by {notice.submitted_by_name}. </>}
            {notice.approved_by_name && <>Approved by {notice.approved_by_name}. </>}
            The person who wrote or submitted a response cannot approve it.
          </p>
        )}
      </section>

      <div className="mt-5 space-y-5">
        <NoticeDetailsCard
          key={`details-${notice.updated_at}`}
          notice={notice}
          types={typesQuery.data ?? []}
          gstins={clientQuery.data?.gstins ?? []}
          staff={staff}
          onChanged={onChanged}
        />
        <NoticeReadingPanel key={`reading-${notice.updated_at}`} notice={notice} onChanged={onChanged} />
        <NoticeDraftPanel key={`draft-${notice.updated_at}`} notice={notice} onChanged={onChanged} />
        <NoticeHistory noticeId={notice.id} version={notice.updated_at} />
      </div>

      <div className="mt-6 flex flex-wrap items-center gap-3">
        {notice.file_available && (
          <button
            onClick={() => void onDownload()}
            className="rounded border border-slate-300 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50"
          >
            Download the original
          </button>
        )}
        {hasRole("MANAGER") && (
          <button
            onClick={() => {
              if (window.confirm("Remove this notice? It is kept in the audit trail but disappears from every list.")) {
                remove.mutate();
              }
            }}
            disabled={remove.isPending}
            className="ml-auto rounded border border-rose-200 px-4 py-2 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
          >
            Delete
          </button>
        )}
      </div>
      {actionError && (
        <p role="alert" className="mt-3 text-sm text-rose-700">
          {actionError}
        </p>
      )}
    </main>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className={mono ? "font-mono text-slate-800" : "text-slate-800"}>{value}</dd>
    </div>
  );
}
