import { useState } from "react";
import type { ExceptionStatus, ReconciliationResult } from "../types";
import { STATUS_CLASSES, STATUS_LABELS, formatINR } from "../statusMeta";
import { useAuth } from "../auth/AuthContext";
import { formatDate } from "../utils/time";

interface Props {
  result: ReconciliationResult;
  expanded: boolean;
  onToggle: () => void;
  onResolve: (exceptionId: string, note: string) => Promise<void>;
  onApprove: (exceptionId: string, approved: boolean, reworkReason?: string) => Promise<void>;
}

const EXCEPTION_LABELS: Record<ExceptionStatus, string> = {
  OPEN: "Open",
  IN_PROGRESS: "In progress",
  PENDING_APPROVAL: "Awaiting approval",
  APPROVED: "Approved",
  REWORK: "Sent back",
  AUTO_RESOLVED: "Auto-resolved",
};

const EXCEPTION_CLASSES: Record<ExceptionStatus, string> = {
  OPEN: "text-slate-500",
  IN_PROGRESS: "text-sky-700",
  PENDING_APPROVAL: "text-amber-700",
  APPROVED: "text-emerald-700",
  REWORK: "text-rose-700",
  AUTO_RESOLVED: "text-emerald-600",
};

function DiffField({
  label,
  bookValue,
  gstrValue,
  isCurrency,
}: {
  label: string;
  bookValue: string | number | null | undefined;
  gstrValue: string | number | null | undefined;
  isCurrency?: boolean;
}) {
  const differs =
    bookValue !== undefined &&
    gstrValue !== undefined &&
    bookValue !== null &&
    gstrValue !== null &&
    String(bookValue) !== String(gstrValue);
  const fmt = (v: string | number | null | undefined) =>
    v === null || v === undefined ? "—" : isCurrency ? formatINR(Number(v)) : String(v);
  return (
    <div className="grid grid-cols-3 gap-2 border-b border-slate-100 py-1.5 text-sm last:border-0">
      <div className="text-slate-500">{label}</div>
      <div className={differs ? "font-medium text-rose-600" : "text-slate-800"}>
        {fmt(bookValue)}
      </div>
      <div className={differs ? "font-medium text-rose-600" : "text-slate-800"}>
        {fmt(gstrValue)}
      </div>
    </div>
  );
}

export default function ExceptionRow({
  result,
  expanded,
  onToggle,
  onResolve,
  onApprove,
}: Props) {
  const exception = result.exception ?? null;
  const [note, setNote] = useState(exception?.resolution_notes ?? "");
  const [reworkReason, setReworkReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const { hasRole, user } = useAuth();

  const p = result.purchase_entry;
  const g = result.gstr2b_entry;
  const vendor = p?.vendor_name ?? g?.vendor_name ?? g?.vendor_gstin ?? "—";
  const status: ExceptionStatus = exception?.status ?? "OPEN";

  // A resolution is a proposal until someone else approves it. Whoever wrote
  // it cannot also sign it off, so the approval controls are hidden from them.
  const canApprove =
    hasRole("MANAGER") &&
    status === "PENDING_APPROVAL" &&
    exception?.resolved_by !== user?.id;

  const locked = status === "APPROVED";

  async function run(fn: () => Promise<void>) {
    setSaving(true);
    setActionError(null);
    try {
      await fn();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <tr
        onClick={onToggle}
        className="cursor-pointer border-b border-slate-100 hover:bg-slate-50"
      >
        <td className="px-3 py-2">
          <span
            className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${STATUS_CLASSES[result.match_status]}`}
          >
            {STATUS_LABELS[result.match_status]}
          </span>
        </td>
        <td className="px-3 py-2 text-sm text-slate-800">{vendor}</td>
        <td className="px-3 py-2 text-sm text-slate-600">
          {p?.invoice_number ?? g?.invoice_number ?? "—"}
        </td>
        <td className="px-3 py-2 text-right text-sm text-slate-800">
          {formatINR(p?.taxable_value ?? g?.taxable_value)}
        </td>
        <td className="px-3 py-2 text-right text-sm text-slate-500">
          {result.confidence_score}%
        </td>
        <td className="px-3 py-2 text-center">
          <span className={`text-xs font-medium ${EXCEPTION_CLASSES[status]}`}>
            {EXCEPTION_LABELS[status]}
          </span>
        </td>
      </tr>

      {expanded && (
        <tr className="border-b border-slate-100 bg-slate-50">
          <td colSpan={6} className="px-6 py-4">
            <p className="mb-3 text-sm text-slate-700">{result.plain_language_note}</p>

            {exception?.itc_lapse_deadline && (
              <p className="mb-3 text-xs text-slate-600">
                Input tax credit on this invoice lapses on{" "}
                <span className="font-medium">
                  {formatDate(exception.itc_lapse_deadline)}
                </span>
                .
              </p>
            )}

            <div className="grid grid-cols-3 gap-2 rounded-md border border-slate-200 bg-white p-3">
              <div className="text-xs font-semibold uppercase text-slate-400">Field</div>
              <div className="text-xs font-semibold uppercase text-slate-400">
                Books (purchase register)
              </div>
              <div className="text-xs font-semibold uppercase text-slate-400">GSTR-2B</div>
              <DiffField label="Vendor GSTIN" bookValue={p?.vendor_gstin} gstrValue={g?.vendor_gstin} />
              <DiffField label="Invoice #" bookValue={p?.invoice_number} gstrValue={g?.invoice_number} />
              <DiffField label="Invoice date" bookValue={p?.invoice_date} gstrValue={g?.invoice_date} />
              <DiffField label="Taxable value" bookValue={p?.taxable_value} gstrValue={g?.taxable_value} isCurrency />
              <DiffField label="CGST" bookValue={p?.cgst} gstrValue={g?.cgst} isCurrency />
              <DiffField label="SGST" bookValue={p?.sgst} gstrValue={g?.sgst} isCurrency />
              <DiffField label="IGST" bookValue={p?.igst} gstrValue={g?.igst} isCurrency />
              <DiffField label="Total value" bookValue={p?.total_value} gstrValue={g?.total_value} isCurrency />
            </div>

            {exception?.rework_reason && (
              <p className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
                Sent back for rework: {exception.rework_reason}
              </p>
            )}

            {actionError && (
              <p role="alert" className="mt-3 text-sm text-rose-700">
                {actionError}
              </p>
            )}

            {exception && !locked && (
              <div className="mt-3 flex items-start gap-2">
                <textarea
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="Resolution note, e.g. 'Confirmed timing difference, vendor filed late'"
                  className="min-h-15 flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                />
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    void run(() => onResolve(exception.id, note));
                  }}
                  disabled={saving || !note.trim()}
                  className="shrink-0 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {saving ? "Saving…" : "Submit for approval"}
                </button>
              </div>
            )}

            {exception && canApprove && (
              <div className="mt-3 rounded border border-slate-200 bg-white p-3">
                <p className="text-sm font-medium text-slate-800">Review this resolution</p>
                <p className="mt-1 text-sm text-slate-600">{exception.resolution_notes}</p>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      void run(() => onApprove(exception.id, true));
                    }}
                    disabled={saving}
                    className="rounded bg-emerald-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-700 disabled:opacity-50"
                  >
                    Approve
                  </button>
                  <input
                    value={reworkReason}
                    onChange={(e) => setReworkReason(e.target.value)}
                    placeholder="Reason for sending back"
                    className="min-w-56 flex-1 rounded border border-slate-300 px-2 py-1.5 text-sm"
                  />
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      void run(() => onApprove(exception.id, false, reworkReason));
                    }}
                    disabled={saving || !reworkReason.trim()}
                    className="rounded border border-rose-300 px-3 py-1.5 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                  >
                    Send back
                  </button>
                </div>
              </div>
            )}

            {locked && (
              <p className="mt-3 text-sm text-emerald-700">
                Approved{exception?.approved_at
                  ? ` on ${formatDate(exception.approved_at)}`
                  : ""}
                . This resolution is locked.
              </p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
