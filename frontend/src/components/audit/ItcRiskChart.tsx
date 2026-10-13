import { useState } from "react";
import type { AuditBriefPoint } from "../../types";
import { formatRupees, formatPeriod } from "../../noticeMeta";

const BAR_MAX_WIDTH = 22;
const GAP = 6;
const CHART_HEIGHT = 140;
const AXIS_HEIGHT = 22;

/**
 * ITC at risk, one bar per period. A single series needs no legend — the
 * title says what is plotted. Periods with no reconciliation at all are
 * drawn as a hollow dashed slot, not a zero-height bar: "nothing was at
 * risk" and "nothing was reconciled" are different findings and must not
 * look the same on the chart.
 */
export default function ItcRiskChart({ points }: { points: AuditBriefPoint[] }) {
  const [hover, setHover] = useState<number | null>(null);
  if (points.length === 0) return null;

  const values = points.map((p) => Number(p.itc_at_risk));
  const max = Math.max(1, ...values);
  const width = points.length * BAR_MAX_WIDTH + (points.length - 1) * GAP;
  const scale = (v: number) => (v <= 0 ? 0 : (v / max) * (CHART_HEIGHT - 8));

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${width} ${CHART_HEIGHT + AXIS_HEIGHT}`}
        width="100%"
        height={CHART_HEIGHT + AXIS_HEIGHT}
        role="img"
        aria-label="ITC at risk by period"
        className="overflow-visible"
      >
        {/* baseline */}
        <line
          x1={0} x2={width} y1={CHART_HEIGHT} y2={CHART_HEIGHT}
          stroke="currentColor" strokeWidth={1} className="text-slate-200"
        />
        {points.map((p, i) => {
          const x = i * (BAR_MAX_WIDTH + GAP);
          const value = values[i];
          const h = scale(value);
          const isHover = hover === i;
          const cx = x + BAR_MAX_WIDTH / 2;

          return (
            <g key={p.period}>
              {/* generous hit target, bigger than the bar itself */}
              <rect
                x={x - GAP / 2} y={0} width={BAR_MAX_WIDTH + GAP} height={CHART_HEIGHT}
                fill="transparent"
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover(null)}
                onFocus={() => setHover(i)}
                onBlur={() => setHover(null)}
                tabIndex={0}
                role="button"
                aria-label={`${formatPeriod(p.period)}: ${p.reconciled ? formatRupees(p.itc_at_risk) + " at risk" : "not reconciled"}`}
              />
              {!p.reconciled ? (
                <rect
                  x={x} y={CHART_HEIGHT - 10} width={BAR_MAX_WIDTH} height={10}
                  rx={2} fill="none" strokeDasharray="3 2" strokeWidth={1.5}
                  className={isHover ? "stroke-slate-400" : "stroke-slate-300"}
                />
              ) : (
                <rect
                  x={x} y={CHART_HEIGHT - h} width={BAR_MAX_WIDTH} height={Math.max(h, value > 0 ? 3 : 0)}
                  rx={4}
                  className={
                    value > 0
                      ? isHover ? "fill-rose-500" : "fill-rose-400"
                      : isHover ? "fill-emerald-400" : "fill-emerald-300"
                  }
                />
              )}
              <text
                x={cx} y={CHART_HEIGHT + 15} textAnchor="middle"
                className="fill-slate-500 text-[8px]"
              >
                {formatPeriod(p.period).replace(/ \d{4}$/, "")}
              </text>
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div
          className="pointer-events-none absolute rounded border border-slate-200 bg-white px-2 py-1 text-xs shadow-md"
          style={{
            left: `${((hover * (BAR_MAX_WIDTH + GAP) + BAR_MAX_WIDTH / 2) / width) * 100}%`,
            top: 0, transform: "translate(-50%, -100%)",
          }}
        >
          <div className="font-medium text-slate-900">{formatPeriod(points[hover].period)}</div>
          {points[hover].reconciled ? (
            <div className="text-rose-700">{formatRupees(points[hover].itc_at_risk)} at risk</div>
          ) : (
            <div className="text-slate-500">Not reconciled</div>
          )}
        </div>
      )}
    </div>
  );
}
