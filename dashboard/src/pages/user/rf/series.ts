import type { SnrSeries } from "../../../types";

/**
 * An owned node's SNR series as the chart draws it, and the times around it.
 * Clock times are the viewer's own, on a 24-hour clock: an axis of "02 PM"
 * ticks reads worse than one of "14:00", and the two halves of the day are
 * one axis apart.
 */

const HOUR_MS = 3_600_000;

/** One bucket on the chart. A gap stands where the server sent no bucket, so
 *  the lines break there rather than drawing straight through an hour that
 *  had no detection. */
export interface SnrBucket {
  /** The bucket's start, epoch ms. */
  t_ms: number;
  median_db: number | null;
  p90_db: number | null;
  count: number;
  /** For a gap: where the next bucket with detections starts, epoch ms. */
  gap_until?: number;
}

/** The series' points, oldest first, with a gap after any bucket the next one
 *  does not follow on from. One gap is enough to break a line, however many
 *  buckets went empty. */
export function withGaps(series: SnrSeries): SnrBucket[] {
  const step = series.bucket_s * 1000;
  const points = [...series.points].sort((a, b) => a.t_ms - b.t_ms);
  const buckets: SnrBucket[] = [];
  points.forEach((point, i) => {
    const previous = points[i - 1];
    if (previous && step > 0 && point.t_ms - previous.t_ms > step) {
      buckets.push({ t_ms: previous.t_ms + step, median_db: null, p90_db: null, count: 0, gap_until: point.t_ms });
    }
    buckets.push({ t_ms: point.t_ms, median_db: point.median_db, p90_db: point.p90_db, count: point.count });
  });
  return buckets;
}

/** Whether the line through `key` has nothing either side of this bucket, so
 *  that only a dot can show it. */
export function isolated(buckets: SnrBucket[], index: number, key: "median_db" | "p90_db"): boolean {
  if (buckets[index]?.[key] == null) return false;
  return buckets[index - 1]?.[key] == null && buckets[index + 1]?.[key] == null;
}

/** Ticks on whole local hours across [start, end]: every hour of a short
 *  window, every three of a day, every six past that. */
export function hourTicks(start: number, end: number): number[] {
  const span = end - start;
  if (!Number.isFinite(span) || span <= 0) return [];
  const every = span <= 6 * HOUR_MS ? 1 : span <= 24 * HOUR_MS ? 3 : 6;
  const at = new Date(start);
  at.setMinutes(0, 0, 0);
  if (at.getTime() < start) at.setHours(at.getHours() + 1);
  // setHours, not a sum of milliseconds, so a change of clocks keeps the
  // ticks on the hour.
  while (at.getHours() % every !== 0) at.setHours(at.getHours() + 1);
  const ticks: number[] = [];
  for (; at.getTime() <= end; at.setHours(at.getHours() + every)) ticks.push(at.getTime());
  return ticks;
}

const CLOCK: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
const CLOCK_SECONDS: Intl.DateTimeFormatOptions = { ...CLOCK, second: "2-digit" };

/** A moment as HH:MM. */
export function clock(ms: number): string {
  return new Date(ms).toLocaleTimeString([], CLOCK);
}

/** A moment as HH:MM:SS, for samples taken seconds apart. */
export function clockSeconds(ms: number): string {
  return new Date(ms).toLocaleTimeString([], CLOCK_SECONDS);
}

/** "the last 24 hours", "the last hour". */
export function lastHours(hours: number): string {
  return hours === 1 ? "the last hour" : `the last ${hours} hours`;
}

/** The chart's title: what it shows over which window, in which buckets. */
export function seriesTitle(hours: number, bucketS: number): string {
  const bucket = bucketS % 60 === 0 ? `${bucketS / 60}-minute` : `${bucketS}-second`;
  return `SNR, ${lastHours(hours).replace(/^the /, "")} · ${bucket} buckets`;
}

/** How recent the series is: its newest detection, with the date when that
 *  was not on the day the answer came. */
export function throughLabel(throughMs: number, answeredAt: number): string {
  const through = new Date(throughMs);
  const sameDay = through.toDateString() === new Date(answeredAt).toDateString();
  const day = sameDay ? "" : ` on ${through.toLocaleDateString([], { day: "numeric", month: "short" })}`;
  return `through ${clock(throughMs)}${day}`;
}
