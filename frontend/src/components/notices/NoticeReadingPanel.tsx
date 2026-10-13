import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { analyseNotice, getErrorMessage, putNoticeText } from "../../api/client";
import type { NoticeDetail } from "../../types";
import { formatDateTime } from "../../utils/time";

const CONFIDENCE_CLASS = {
  high: "border-emerald-200 bg-emerald-50 text-emerald-700",
  medium: "border-amber-200 bg-amber-50 text-amber-800",
  low: "border-rose-200 bg-rose-50 text-rose-700",
} as const;

/**
 * How the notice was read: where its text came from, what the analyser made of
 * it, and a way to supply the text by hand when it could not be read.
 */
export default function NoticeReadingPanel({
  notice,
  onChanged,
}: {
  notice: NoticeDetail;
  onChanged: (n: NoticeDetail) => void;
}) {
  const needsText =
    !notice.ocr_text &&
    (notice.extraction_status === "NEEDS_OCR" || notice.extraction_status === "FAILED");
  const [editing, setEditing] = useState(needsText);
  const [text, setText] = useState(notice.ocr_text ?? "");
  const [error, setError] = useState<string | null>(null);

  const facts = notice.ai_analysis?.facts;
  const canEditText = notice.editable.text;

  const saveText = useMutation({
    mutationFn: () => putNoticeText(notice.id, text),
    onSuccess: (n) => {
      setEditing(false);
      setError(null);
      onChanged(n);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  const analyse = useMutation({
    mutationFn: (overwrite: boolean) => analyseNotice(notice.id, overwrite),
    onSuccess: (n) => {
      setError(null);
      onChanged(n);
    },
    onError: (err) => setError(getErrorMessage(err)),
  });

  function reanalyse() {
    const overwrite = window.confirm(
      "Read the notice again?\n\nOK replaces every detail with the new reading, including any " +
        "you have corrected.\nCancel keeps your edits and only fills in blanks.",
    );
    analyse.mutate(overwrite);
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-semibold text-slate-900">What the notice says</h2>
        {facts && (
          <span
            className={`rounded border px-2 py-0.5 text-xs font-medium ${CONFIDENCE_CLASS[facts.confidence]}`}
            title="The analyser's own confidence in its reading"
          >
            {facts.confidence} confidence
          </span>
        )}
        {canEditText && (notice.ocr_text || editing) && (
          <button
            onClick={reanalyse}
            disabled={analyse.isPending || !notice.ocr_text}
            className="ml-auto rounded border border-slate-300 px-3 py-1 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-40"
          >
            {analyse.isPending ? "Reading…" : facts ? "Read again" : "Read the notice"}
          </button>
        )}
      </div>

      {notice.extraction_message && (
        <p className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          {notice.extraction_message}
        </p>
      )}
      {notice.analysis_status === "FAILED" && notice.analysis_error && (
        <p role="alert" className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          The notice could not be analysed: {notice.analysis_error} The details below were not filled
          in; enter them by hand, or use “Read the notice” to try again.
        </p>
      )}

      {facts && (
        <div className="mt-3 space-y-3 text-sm">
          {facts.summary && <p className="text-slate-700">{facts.summary}</p>}
          {facts.warnings.length > 0 && (
            <ul className="list-disc space-y-0.5 pl-5 text-amber-800">
              {facts.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          {facts.questions.length > 0 && (
            <div>
              <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
                What the notice asks for
              </div>
              <ol className="mt-1 list-decimal space-y-1 pl-5 text-slate-800">
                {facts.questions.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ol>
            </div>
          )}
          <p className="text-xs text-slate-500">
            Read by {notice.ai_analysis?.provider === "mock" ? "the local rule-based reader" : notice.ai_analysis?.model}
            {notice.analysed_at ? ` on ${formatDateTime(notice.analysed_at)}` : ""}.
          </p>
        </div>
      )}

      <div className="mt-4 border-t border-slate-100 pt-3">
        <div className="flex flex-wrap items-center gap-3">
          <button
            onClick={() => setEditing((v) => !v)}
            className="text-sm text-slate-700 underline hover:text-slate-900"
          >
            {editing ? "Hide text" : notice.ocr_text ? "Show the notice text" : "Enter the notice text"}
          </button>
          {notice.extraction_method && (
            <span className="text-xs text-slate-500">
              Source:{" "}
              {{ PDF_TEXT: "text layer of the PDF", OCR: "OCR of an image (may contain errors)", MANUAL: "typed in" }[
                notice.extraction_method
              ] ?? notice.extraction_method}
            </span>
          )}
        </div>

        {editing && (
          <div className="mt-2">
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              readOnly={!canEditText}
              rows={12}
              aria-label="Notice text"
              className="w-full rounded border border-slate-300 px-3 py-2 font-mono text-xs"
              placeholder="Paste the full text of the notice here."
            />
            {canEditText && (
              <div className="mt-2 flex items-center gap-3">
                <button
                  onClick={() => saveText.mutate()}
                  disabled={saveText.isPending || text.trim().length < 20 || text === (notice.ocr_text ?? "")}
                  className="rounded bg-slate-900 px-4 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-40"
                >
                  {saveText.isPending ? "Saving…" : "Save text"}
                </button>
                <span className="text-xs text-slate-500">
                  After saving, use “Read the notice” to fill in the details from this text.
                </span>
              </div>
            )}
          </div>
        )}
      </div>

      {error && (
        <p role="alert" className="mt-3 text-sm text-rose-700">
          {error}
        </p>
      )}
    </section>
  );
}
