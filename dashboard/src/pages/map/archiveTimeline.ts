/**
 * Time and availability arithmetic for the archive bar, kept out of the
 * components so it can be tested without mounting them or the vendored
 * timeline.
 *
 * Everything here is UTC: the URL and the API speak UTC, and the visitor's
 * own offset only ever appears as the "local" text beside the field.
 */

import type { TimelineRow } from "@edsc/timeline";

export const HOUR_MS = 3_600_000;
const MINUTE_MS = 60_000;

/** One hour of the availability endpoint. Declared here rather than imported
 *  from replay/, which another part of the feature owns. */
export interface AvailabilityHourLike {
  start_ms: number;
  count: number;
  unclassified: number;
}

/** Sorted, with overlapping and touching intervals joined. Hours that abut
 *  draw as one bar, so a busy evening reads as a block rather than a comb. */
export function mergeIntervals(intervals: [number, number][]): [number, number][] {
  const out: [number, number][] = [];
  for (const [start, end] of intervals.slice().sort((a, b) => a[0] - b[0])) {
    const last = out[out.length - 1];
    if (last && start <= last[1]) last[1] = Math.max(last[1], end);
    else out.push([start, end]);
  }
  return out;
}

const hoursWhere = (hours: AvailabilityHourLike[], has: (h: AvailabilityHourLike) => boolean) =>
  mergeIntervals(
    hours.filter(has).map((h): [number, number] => [h.start_ms, h.start_ms + HOUR_MS]),
  );

/**
 * The strip's rows: published solves, and beside them the rows written before
 * the fleet column existed, which no fleet filter can vouch for. An hour the
 * list leaves out, or one with nothing in either column, draws nothing: the
 * gap is the information.
 */
export function availabilityRows(hours: AvailabilityHourLike[]): TimelineRow[] {
  const rows: TimelineRow[] = [
    {
      id: "solves",
      title: "Solves",
      color: "var(--accent)",
      intervals: hoursWhere(hours, (h) => h.count > 0),
    },
  ];
  const unclassified = hoursWhere(hours, (h) => h.unclassified > 0);
  if (unclassified.length) {
    rows.push({
      id: "unclassified",
      title: "Unclassified (fleet unknown)",
      color: "var(--warning)",
      intervals: unclassified,
    });
  }
  return rows;
}

/**
 * The instant a timeline gesture names: the middle of the span it reports,
 * to the minute, and never past `nowMs` (the archive holds nothing later).
 * Null when the gesture carried no span, as a cleared selection does.
 */
export function instantFromSpan(
  start: number | null | undefined,
  end: number | null | undefined,
  nowMs: number,
): number | null {
  if (typeof start !== "number" || typeof end !== "number") return null;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  const mid = Math.round((start + end) / 2 / MINUTE_MS) * MINUTE_MS;
  return Math.min(mid, nowMs);
}

const iso = (ms: number) => (Number.isFinite(ms) ? new Date(ms).toISOString() : "");

/** `YYYY-MM-DDTHH:MM:SS` in UTC, floored to the second: a datetime-local value. */
export function formatUtcInput(ms: number): string {
  return iso(ms).slice(0, 19);
}

/** `HH:MM:SS` in UTC. */
export function formatUtcClock(ms: number): string {
  return iso(ms).slice(11, 19);
}

/** `HH:MM` in UTC. */
export function formatUtcHm(ms: number): string {
  return iso(ms).slice(11, 16);
}

// What a datetime-local field can hand back: the browser normalises the value,
// dropping the seconds when they are zero and, in some engines, adding
// milliseconds when they are not. A space separator is valid input too.
const LOCAL_DATE_TIME = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/;

/** The field's value read as UTC, in epoch ms, or null when it is not a
 *  complete, real date and time (an empty or half-typed field, 30 February). */
export function parseUtcInput(str: string): number | null {
  const m = LOCAL_DATE_TIME.exec((str ?? "").trim());
  if (!m) return null;
  const [year, month, day, hour, minute, second] = m.slice(1, 7).map((v) => Number(v ?? 0));
  const millis = m[7] ? Number(m[7].padEnd(3, "0")) : 0;
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return null;
  const ms = Date.UTC(year, month - 1, day, hour, minute, second, millis);
  // Date.UTC rolls 30 February over into March; a real date survives the trip.
  const back = new Date(ms);
  if (back.getUTCFullYear() !== year || back.getUTCMonth() !== month - 1 || back.getUTCDate() !== day) {
    return null;
  }
  return ms;
}

/** `UTC+3`, `UTC-4`, `UTC+5:30`, `UTC+0`, for an offset in minutes east of UTC. */
export function formatUtcOffset(offsetMin: number): string {
  const sign = offsetMin < 0 ? "-" : "+";
  const abs = Math.abs(Math.round(offsetMin));
  const h = Math.floor(abs / 60);
  const m = abs % 60;
  return `UTC${sign}${h}${m ? `:${String(m).padStart(2, "0")}` : ""}`;
}

/**
 * The visitor's wall clock at `ms`, for the text beside the UTC field:
 * `local 23:14, UTC+3`. The offset is the one in force at that instant, so a
 * replay from before a daylight-saving change quotes the offset it had then.
 * `offsetMin` (minutes east of UTC) is for tests; it defaults to the browser's.
 */
export function formatLocalBeside(ms: number, offsetMin?: number): string {
  if (!Number.isFinite(ms)) return "";
  const off = offsetMin ?? -new Date(ms).getTimezoneOffset();
  return `local ${formatUtcHm(ms + off * MINUTE_MS)}, ${formatUtcOffset(off)}`;
}

/**
 * `HH:MM:SS` on the visitor's wall clock at `ms`, with the same offset rules as
 * `formatLocalBeside`: the one in force at that instant, `offsetMin` for tests.
 */
export function formatLocalClock(ms: number, offsetMin?: number): string {
  if (!Number.isFinite(ms)) return "";
  const off = offsetMin ?? -new Date(ms).getTimezoneOffset();
  return formatUtcClock(ms + off * MINUTE_MS);
}
