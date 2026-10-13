import { PIPELINE, STATUS_LABEL } from "../../noticeMeta";
import type { NoticeStatus } from "../../types";

/** The six stages, with the current one marked and earlier ones ticked. */
export default function NoticePipeline({ status }: { status: NoticeStatus }) {
  const current = PIPELINE.indexOf(status);
  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-2 text-xs" aria-label="Notice progress">
      {PIPELINE.map((step, i) => {
        const done = i < current;
        const active = i === current;
        return (
          <li key={step} className="flex items-center gap-1" aria-current={active ? "step" : undefined}>
            <span
              className={`rounded-full border px-3 py-1 font-medium ${
                active
                  ? "border-slate-900 bg-slate-900 text-white"
                  : done
                    ? "border-emerald-200 bg-emerald-50 text-emerald-800"
                    : "border-slate-200 bg-white text-slate-500"
              }`}
            >
              {done ? "✓ " : ""}
              {STATUS_LABEL[step]}
            </span>
            {i < PIPELINE.length - 1 && <span className="text-slate-300">›</span>}
          </li>
        );
      })}
    </ol>
  );
}
