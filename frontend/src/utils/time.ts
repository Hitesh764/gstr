/**
 * Date handling for values that come from the API.
 *
 * The backend stores and returns naive UTC datetimes, serialised WITHOUT a
 * timezone suffix ("2026-09-21T12:00:00"). Browsers read a suffix-less
 * date-time as LOCAL time, so `new Date(value)` is wrong by the viewer's UTC
 * offset. For someone in India that is 5 hours 30 minutes: a notification
 * created a second ago reads "5h ago", and a consent that ends at midnight
 * UTC is shown as ending at 05:30 the same morning.
 *
 * Everything that displays an API timestamp goes through `parseUtc`.
 *
 * Date-only values ("2026-11-30", such as an ITC lapse deadline) are the
 * opposite case: they are calendar dates with no time and no zone, so they are
 * read as a local calendar date. Parsing them as UTC midnight shows the wrong
 * day to anyone west of Greenwich.
 */

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/;

export function parseUtc(value: string): Date {
  if (DATE_ONLY.test(value)) {
    const [y, m, d] = value.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  return new Date(HAS_ZONE.test(value) ? value : `${value}Z`);
}

export function formatDateTime(value?: string | null): string {
  if (!value) return "—";
  return parseUtc(value).toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDate(value?: string | null): string {
  if (!value) return "—";
  return parseUtc(value).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/** "5 minutes", "6 hours", "12 days": for how long until something happens. */
export function formatDuration(seconds: number): string {
  if (seconds <= 0) return "now";
  if (seconds < 3600) {
    const m = Math.max(1, Math.round(seconds / 60));
    return `${m} minute${m === 1 ? "" : "s"}`;
  }
  if (seconds < 2 * 86400) {
    const h = Math.round(seconds / 3600);
    return `${h} hour${h === 1 ? "" : "s"}`;
  }
  const d = Math.round(seconds / 86400);
  return `${d} days`;
}

export function relativeTime(iso: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - parseUtc(iso).getTime()) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return formatDate(iso);
}
