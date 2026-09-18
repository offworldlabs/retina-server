/** Which files survive the current filters. The node set is passed in rather
 *  than derived here, because what counts as an effective node depends on the
 *  registry and the radius filter, which are not this module's business. */

import { minuteOfDay } from "./dates";
import type { ArchiveFile } from "./keys";
import { TOD_END, TOD_START, type ExplorerFilters } from "./urlState";

const LAST_MINUTE = 1439;

const minuteOfDayFromMs = (ms: number): number => {
  const d = new Date(ms);
  return d.getUTCHours() * 60 + d.getUTCMinutes();
};

/** Minutes `from` to `to` as spans within one day: two of them when `to` is
 *  earlier, since the range then runs past midnight. */
const spansOf = (from: number, to: number): [number, number][] =>
  from <= to ? [[from, to]] : [[from, LAST_MINUTE], [0, to]];

/** The time-of-day window as spans of minutes: two when it wraps midnight. */
export const todSpans = (todFrom: string, todTo: string): [number, number][] =>
  spansOf(minuteOfDay(todFrom), minuteOfDay(todTo));

/** A file covers an estimated hour, so this asks whether that hour overlaps
 *  the window. Either can wrap midnight: a window of 22:00–02:00 is the
 *  overnight period, and a file written at 00:30 covers the end of one day and
 *  the start of the next. */
export function inTod(file: ArchiveFile, todFrom: string, todTo: string): boolean {
  const windows = todSpans(todFrom, todTo);
  const hour = spansOf(minuteOfDayFromMs(file.startMs), minuteOfDayFromMs(file.endMs));
  return hour.some(([x, y]) => windows.some(([p, q]) => x <= q && y >= p));
}

export function makePredicate(
  filters: ExplorerFilters,
  effective: Set<string>,
): (file: ArchiveFile) => boolean {
  const { minSize, todFrom, todTo } = filters;
  const allHours = todFrom === TOD_START && todTo === TOD_END;
  return (file) =>
    effective.has(file.node) &&
    file.size >= minSize &&
    (allHours || inTod(file, todFrom, todTo));
}
