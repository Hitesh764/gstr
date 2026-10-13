import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { getErrorMessage, transitionNotice } from "../../api/client";
import { STATUS_LABEL, actionLabel } from "../../noticeMeta";
import type { NoticeDetail, NoticeStatus } from "../../types";

/**
 * The moves available from the notice's current stage.
 *
 * Which moves are allowed, and why a blocked one is blocked, come from the
 * server (`notice.actions`), so this screen never offers something the
 * approval rules would refuse and never has to duplicate those rules.
 */
export default function NoticeActionBar({
  notice,
  onChanged,
}: {
  notice: NoticeDetail;
  onChanged: (n: NoticeDetail) => void;
}) {
  const [open, setOpen] = useState<NoticeStatus | null>(null);
  const [reason, setReason] = useState("");
  const [reference, setReference] = useState("");
  const [filedOn, setFiledOn] = useState(() => new Date().toISOString().slice(0, 10));
  const [outcome, setOutcome] = useState("");
  const [error, setError] = useState<string | null>(null);

  const move = useMutation({
    mutationFn: (to: NoticeStatus) =>
      transitionNotice(notice.id, {
        to_status: to,
        reason: reason.trim() || undefined,
        filing_reference: reference.trim() || undefined,
        filed_at: to === "FILED" ? filedOn : undefined,
        outcome: outcome.trim() || undefined,
      }),
    onSuccess: (n) => {
      setOpen(null);
      setReason("");
      setReference("");
      setOutcome("");
      setError(null);
      onChanged(n);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  if (notice.actions.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        This notice is closed{notice.outcome ? `: ${notice.outcome}` : "."}
      </p>
    );
  }

  const blocked = notice.actions.filter((a) => !a.allowed && a.blocked_reason);

  function choose(to: NoticeStatus) {
    setError(null);
    const needsInput =
      to === "FILED" || to === "CLOSED" || (to === "UNDER_REVIEW" && notice.status === "RESPONSE_DRAFTED");
    if (needsInput) setOpen(open === to ? null : to);
    else move.mutate(to);
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-3">
        {notice.actions.map((a) => (
          <button
            key={a.to}
            onClick={() => choose(a.to)}
            disabled={!a.allowed || move.isPending}
            title={a.allowed ? undefined : (a.blocked_reason ?? undefined)}
            className={`rounded px-4 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-40 ${
              a.to === "UNDER_REVIEW" && notice.status === "RESPONSE_DRAFTED"
                ? "border border-slate-300 text-slate-700 hover:bg-slate-50"
                : "bg-slate-900 text-white hover:bg-slate-800"
            }`}
          >
            {actionLabel(notice.status, a.to)}
          </button>
        ))}
        <span className="text-xs text-slate-500">
          Currently {STATUS_LABEL[notice.status].toLowerCase()}
        </span>
      </div>

      {blocked.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs text-slate-500">
          {blocked.map((a) => (
            <li key={a.to}>
              <span className="font-medium">{actionLabel(notice.status, a.to)}:</span> {a.blocked_reason}
            </li>
          ))}
        </ul>
      )}

      {open === "UNDER_REVIEW" && (
        <InlineForm
          submitLabel="Send back"
          pending={move.isPending}
          onSubmit={() => move.mutate("UNDER_REVIEW")}
          onCancel={() => setOpen(null)}
        >
          <label className="block text-xs font-medium text-slate-700" htmlFor="sb-reason">
            What needs to change? (required unless you submitted it yourself)
          </label>
          <textarea
            id="sb-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={3}
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
          />
        </InlineForm>
      )}

      {open === "FILED" && (
        <InlineForm
          submitLabel="Record as filed"
          pending={move.isPending}
          onSubmit={() => move.mutate("FILED")}
          onCancel={() => setOpen(null)}
        >
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="block text-xs font-medium text-slate-700" htmlFor="f-date">
                Date filed on the portal
              </label>
              <input
                id="f-date"
                type="date"
                value={filedOn}
                max={new Date().toISOString().slice(0, 10)}
                onChange={(e) => setFiledOn(e.target.value)}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700" htmlFor="f-ref">
                Acknowledgement or reference number
              </label>
              <input
                id="f-ref"
                value={reference}
                onChange={(e) => setReference(e.target.value)}
                maxLength={100}
                className="mt-1 w-full rounded border border-slate-300 px-3 py-2 font-mono text-sm"
              />
            </div>
          </div>
        </InlineForm>
      )}

      {open === "CLOSED" && (
        <InlineForm
          submitLabel="Close notice"
          pending={move.isPending}
          onSubmit={() => move.mutate("CLOSED")}
          onCancel={() => setOpen(null)}
        >
          <label className="block text-xs font-medium text-slate-700" htmlFor="c-outcome">
            Outcome
          </label>
          <textarea
            id="c-outcome"
            value={outcome}
            onChange={(e) => setOutcome(e.target.value)}
            rows={3}
            placeholder="For example: proceedings dropped; or demand confirmed, appeal to be filed."
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
          />
        </InlineForm>
      )}

      {error && (
        <p role="alert" className="mt-3 text-sm text-rose-700">
          {error}
        </p>
      )}
    </div>
  );
}

function InlineForm({
  children,
  submitLabel,
  pending,
  onSubmit,
  onCancel,
}: {
  children: React.ReactNode;
  submitLabel: string;
  pending: boolean;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-3">
      {children}
      <div className="mt-3 flex gap-2">
        <button
          onClick={onSubmit}
          disabled={pending}
          className="rounded bg-slate-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {pending ? "Working…" : submitLabel}
        </button>
        <button
          onClick={onCancel}
          className="rounded border border-slate-300 px-4 py-1.5 text-sm text-slate-700 hover:bg-white"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
