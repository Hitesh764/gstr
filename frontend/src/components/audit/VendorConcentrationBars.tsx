import type { AuditBriefVendor } from "../../types";
import { formatRupees } from "../../noticeMeta";

/**
 * Share of ITC claimed, top vendors first. One sequential hue (magnitude,
 * not identity), thin bars, value at the tip — a plain HTML/CSS bar rather
 * than SVG, since a horizontal fill is all this needs.
 */
export default function VendorConcentrationBars({ vendors }: { vendors: AuditBriefVendor[] }) {
  if (vendors.length === 0) {
    return <p className="text-sm text-slate-500">No vendor activity in this window.</p>;
  }
  const max = Math.max(...vendors.map((v) => v.share), 1);

  return (
    <ul className="space-y-2.5">
      {vendors.map((v) => (
        <li key={v.vendor_gstin}>
          <div className="flex items-baseline justify-between gap-2 text-sm">
            <span className="truncate font-medium text-slate-800">
              {v.vendor_name || v.vendor_gstin}
            </span>
            <span className="shrink-0 tabular-nums text-slate-600">
              {v.share.toFixed(1)}% · {formatRupees(v.itc_claimed)}
            </span>
          </div>
          <div className="mt-1 h-2 w-full rounded-full bg-slate-100">
            <div
              className="h-2 rounded-full bg-sky-500"
              style={{ width: `${Math.max(2, (v.share / max) * 100)}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}
