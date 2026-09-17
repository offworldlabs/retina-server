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

/** A file covers an estimated hour, so this asks whether that hour overlaps
 *  the window. An hour straddling midnight is two spans: without the split, a
 *  file written at 00:30 would fall outside every window ending at 23:59. */
export function inTod(file: ArchiveFile, todFrom: string, todTo: string): boolean {
  const lo = minuteOfDay(todFrom);
  const hi = minuteOfDay(todTo);
  if (lo === 0 && hi >= LAST_MINUTE) return true;

  const a = minuteOfDayFromMs(file.startMs);
  const b = minuteOfDayFromMs(file.endMs);
  // The window itself never wraps: `todFrom` later than `todTo` selects
  // nothing rather than spanning midnight. Only the file's hour wraps, and
  // that is what the two spans below are for.
  const spans: [number, number][] = a <= b ? [[a, b]] : [[a, LAST_MINUTE], [0, b]];
  return spans.some(([x, y]) => x <= hi && y >= lo);
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
