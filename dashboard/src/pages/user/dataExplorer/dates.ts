/**
 * Day arithmetic for the archive, which is partitioned by UTC day and listed
 * by a `YYYY/MM/DD` prefix. Everything here is UTC: a local-time day boundary
 * would put a file in the wrong bucket for anyone not on Greenwich.
 */

export const DAY_MS = 86_400_000;

/** The ceiling on a single range, so a hand-edited query string cannot queue
 *  a fetch per day for a decade. */
export const MAX_RANGE_DAYS = 400;

export function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function todayUTC(): string {
  return isoDay(new Date());
}

export function addDays(day: string, n: number): string {
  return isoDay(new Date(Date.parse(`${day}T00:00:00Z`) + n * DAY_MS));
}

/** Every day from `from` to `to`, both inclusive, ascending. An inverted range
 *  is empty rather than an error: the caller is usually mid-edit. */
export function daysBetween(from: string, to: string): string[] {
  const start = Date.parse(`${from}T00:00:00Z`);
  const end = Date.parse(`${to}T00:00:00Z`);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return [];
  const days: string[] = [];
  for (let t = start; t <= end && days.length < MAX_RANGE_DAYS; t += DAY_MS) {
    days.push(isoDay(new Date(t)));
  }
  return days;
}

const pad2 = (n: number): string => String(n).padStart(2, "0");

/** "HH:MM" as minutes past midnight. Unparseable parts count as zero, which
 *  keeps a half-typed time from poisoning a comparison with NaN. */
export function minuteOfDay(value: string): number {
  const [h, m] = String(value).split(":");
  return (Number(h) || 0) * 60 + (Number(m) || 0);
}

export function hhmm(ms: number): string {
  const d = new Date(ms);
  return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}
