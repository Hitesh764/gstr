import { useQuery } from "@tanstack/react-query";
import { getErrorMessage, getNoticeHistory } from "../../api/client";
import { AUDIT_LABEL, STATUS_LABEL } from "../../noticeMeta";
import type { NoticeHistoryEntry, NoticeStatus } from "../../types";
import { formatDateTime } from "../../utils/time";

function describe(e: NoticeHistoryEntry): string | null {
  const d = e.details ?? {};
  if (e.action === "NOTICE_STATUS_CHANGED") {
    const from = STATUS_LABEL[d.from as NoticeStatus] ?? String(d.from);
    const to = STATUS_LABEL[d.to as NoticeStatus] ?? String(d.to);
    const extra = d.reason ? ` Reason: ${String(d.reason)}` : d.outcome ? ` Outcome: ${String(d.outcome)}` : "";
    return `${from} → ${to}.${extra}`;
  }
  if (e.action === "NOTICE_UPDATED" && d.changes && typeof d.changes === "object") {
    const fields = Object.keys(d.changes as object).map((k) => k.replace(/_/g, " "));
    return fields.length ? `Changed ${fields.join(", ")}.` : "Deadline confirmed.";
  }
  if (e.action === "NOTICE_ANALYSED") {
    return d.ok === false ? "The analyser could not read it." : `Classified as ${String(d.type ?? "unknown").replace(/_/g, " ").toLowerCase()}.`;
  }
  return null;
}

export default function NoticeHistory({ noticeId, version }: { noticeId: string; version: string }) {
  const history = useQuery({
    // `version` is the notice's updated_at, so the timeline refreshes with every change.
    queryKey: ["notice-history", noticeId, version],
    queryFn: () => getNoticeHistory(noticeId),
  });

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h2 className="text-sm font-semibold text-slate-900">History</h2>
      {history.isLoading && <p className="mt-2 text-sm text-slate-500">Loading…</p>}
      {history.isError && (
        <p role="alert" className="mt-2 text-sm text-rose-700">
          {getErrorMessage(history.error)}
        </p>
      )}
      <ol className="mt-3 space-y-2">
        {history.data
          ?.slice()
          .reverse()
          .map((e) => {
            const detail = describe(e);
            return (
              <li key={e.id} className="text-sm">
                <span className="font-medium text-slate-900">{AUDIT_LABEL[e.action] ?? e.action}</span>
                {detail && <span className="text-slate-700"> · {detail}</span>}
                <div className="text-xs text-slate-500">
                  {e.user_name ?? "System"} · {formatDateTime(e.created_at)}
                </div>
              </li>
            );
          })}
      </ol>
    </section>
  );
}
