import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { getErrorMessage, updateNotice } from "../../api/client";
import type {
  ClientGSTIN,
  NoticeDetail,
  NoticeTypeOption,
  NoticeUpdate,
  User,
} from "../../types";

interface Form {
  title: string;
  notice_type: string;
  notice_reference: string;
  section: string;
  notice_date: string;
  response_deadline: string;
  period: string;
  period_to: string;
  amount_in_question: string;
  client_gstin_id: string;
  assigned_to: string;
}

function fromNotice(n: NoticeDetail): Form {
  return {
    title: n.title ?? "",
    notice_type: n.notice_type ?? "",
    notice_reference: n.notice_reference ?? "",
    section: n.section ?? "",
    notice_date: n.notice_date ?? "",
    response_deadline: n.response_deadline ?? "",
    period: n.period ?? "",
    period_to: n.period_to ?? "",
    amount_in_question: n.amount_in_question ?? "",
    client_gstin_id: n.client_gstin_id ?? "",
    assigned_to: n.assigned_to ?? "",
  };
}

const blankToNull = (v: string) => (v.trim() === "" ? null : v.trim());

/**
 * The facts of the notice, as a person has confirmed them.
 *
 * The deadline gets special handling. When the analyser read it, it is shown
 * with the wording it came from and marked unconfirmed, and the response
 * cannot be submitted until someone confirms it against the original.
 *
 * Remounted by the parent (via `key`) whenever the server copy changes, so
 * the form always starts from what was actually saved.
 */
export default function NoticeDetailsCard({
  notice,
  types,
  gstins,
  staff,
  onChanged,
}: {
  notice: NoticeDetail;
  types: NoticeTypeOption[];
  gstins: ClientGSTIN[];
  staff: User[];
  onChanged: (n: NoticeDetail) => void;
}) {
  const [form, setForm] = useState<Form>(() => fromNotice(notice));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const original = fromNotice(notice);
  const facts = notice.editable.facts;
  const deadlineOpen = notice.editable.deadline;
  const basis = notice.ai_analysis?.facts?.deadline_basis;

  const save = useMutation({
    mutationFn: (body: NoticeUpdate) => {
      return updateNotice(notice.id, body);
    },
    onSuccess: (n) => {
      setError(null);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
      onChanged(n);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  function set<K extends keyof Form>(key: K, value: string) {
    setSaved(false);
    setForm((f) => ({ ...f, [key]: value }));
  }

  function changes(): NoticeUpdate {
    const body: NoticeUpdate = {};
    (Object.keys(form) as (keyof Form)[]).forEach((k) => {
      if (form[k] !== original[k]) {
        (body as Record<string, string | null>)[k] = blankToNull(form[k]);
      }
    });
    return body;
  }

  const pending = changes();
  const dirty = Object.keys(pending).length > 0;

  const input = "mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm disabled:bg-slate-50 disabled:text-slate-500";

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <h2 className="text-sm font-semibold text-slate-900">Details</h2>
      <p className="mt-0.5 text-xs text-slate-500">
        {notice.ai_analysis?.facts
          ? "Read from the notice automatically. Check each field against the original."
          : "Fill these in from the notice."}
      </p>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <Field label="Title" id="d-title">
          <input id="d-title" className={input} value={form.title} disabled={!facts}
            onChange={(e) => set("title", e.target.value)} maxLength={300} />
        </Field>
        <Field label="Type" id="d-type">
          <select id="d-type" className={input} value={form.notice_type} disabled={!facts}
            onChange={(e) => set("notice_type", e.target.value)}>
            <option value="">Not set</option>
            {types.map((t) => (
              <option key={t.code} value={t.code}>{t.label}</option>
            ))}
          </select>
        </Field>
        <Field label="Notice reference" id="d-ref">
          <input id="d-ref" className={`${input} font-mono`} value={form.notice_reference} disabled={!facts}
            onChange={(e) => set("notice_reference", e.target.value)} maxLength={100} />
        </Field>
        <Field label="Section" id="d-section">
          <input id="d-section" className={input} value={form.section} disabled={!facts}
            onChange={(e) => set("section", e.target.value)} placeholder="Section 73" maxLength={100} />
        </Field>
        <Field label="Date of notice" id="d-date">
          <input id="d-date" type="date" className={input} value={form.notice_date} disabled={!facts}
            onChange={(e) => set("notice_date", e.target.value)} />
        </Field>
        <Field label="Amount in question (₹)" id="d-amount">
          <input id="d-amount" inputMode="decimal" className={input} value={form.amount_in_question}
            disabled={!facts} onChange={(e) => set("amount_in_question", e.target.value)} />
        </Field>
        <Field label="Period from" id="d-period">
          <input id="d-period" type="month" className={input} value={form.period} disabled={!facts}
            onChange={(e) => set("period", e.target.value)} />
        </Field>
        <Field label="Period to (if it covers several months)" id="d-period-to">
          <input id="d-period-to" type="month" className={input} value={form.period_to} disabled={!facts}
            onChange={(e) => set("period_to", e.target.value)} />
        </Field>
        <Field label="GSTIN" id="d-gstin">
          <select id="d-gstin" className={`${input} font-mono`} value={form.client_gstin_id} disabled={!facts}
            onChange={(e) => set("client_gstin_id", e.target.value)}>
            <option value="">Not linked</option>
            {gstins.map((g) => (
              <option key={g.id} value={g.id}>{g.gstin}</option>
            ))}
          </select>
        </Field>
        <Field label="Assigned to" id="d-assignee">
          <select id="d-assignee" className={input} value={form.assigned_to}
            onChange={(e) => set("assigned_to", e.target.value)}>
            <option value="">Unassigned</option>
            {staff.map((u) => (
              <option key={u.id} value={u.id}>{u.name}</option>
            ))}
          </select>
        </Field>
      </div>

      <div
        className={`mt-4 rounded border p-3 ${
          notice.deadline_confirmed || !notice.response_deadline
            ? "border-slate-200 bg-slate-50"
            : "border-amber-300 bg-amber-50"
        }`}
      >
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label htmlFor="d-deadline" className="block text-xs font-medium text-slate-700">
              Response deadline
            </label>
            <input id="d-deadline" type="date" className={`${input} w-auto`} value={form.response_deadline}
              disabled={!deadlineOpen} onChange={(e) => set("response_deadline", e.target.value)} />
          </div>
          {notice.response_deadline && !notice.deadline_confirmed && deadlineOpen && !dirty && (
            <button
              onClick={() => save.mutate({ confirm_deadline: true })}
              disabled={save.isPending}
              className="rounded bg-amber-600 px-4 py-2 text-sm font-medium text-white hover:bg-amber-700 disabled:opacity-50"
            >
              I have checked this against the notice
            </button>
          )}
          {notice.deadline_confirmed && (
            <span className="pb-2 text-sm font-medium text-emerald-700">✓ Confirmed</span>
          )}
        </div>
        {notice.response_deadline && !notice.deadline_confirmed && (
          <p className="mt-2 text-xs text-amber-900">
            This date was read by the analyser and has not been checked. A response cannot be
            submitted until you confirm it.
          </p>
        )}
        {basis && (
          <p className="mt-1 text-xs text-slate-600">
            Read from: <em>“{basis}”</em>
          </p>
        )}
        {!notice.response_deadline && (
          <p className="mt-2 text-xs text-slate-600">
            No deadline is set. Enter the date from the notice: deadline alerts depend on it.
          </p>
        )}
      </div>

      {error && (
        <p role="alert" className="mt-3 text-sm text-rose-700">
          {error}
        </p>
      )}
      <div className="mt-4 flex items-center gap-3">
        <button
          onClick={() => save.mutate(pending)}
          disabled={!dirty || save.isPending}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-40"
        >
          {save.isPending ? "Saving…" : "Save changes"}
        </button>
        {saved && <span className="text-sm text-emerald-700">Saved.</span>}
        {!facts && (
          <span className="text-xs text-slate-500">
            Details are locked once a response is drafted. Send it back for review to change them.
          </span>
        )}
      </div>
    </section>
  );
}

function Field({ label, id, children }: { label: string; id: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={id} className="block text-xs font-medium text-slate-700">
        {label}
      </label>
      {children}
    </div>
  );
}
