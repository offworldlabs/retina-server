/**
 * What the availability timeline draws, and how its gestures become filters.
 * Kept apart from the component so the row and range arithmetic can be tested
 * without mounting the vendored timeline.
 */

import type { TimelineChange, TimelineRow } from "@edsc/timeline";

import { DAY_MS, hhmm, isoDay } from "./dates";
import type { ArchiveFile } from "./keys";
import { defaultFilters, TOD_END, TOD_START, type ExplorerFilters } from "./urlState";

/** The component draws at most this many rows, hardcoded (Earthdata Search's
 *  three-collection limit), so past it the rows have to aggregate. */
export const MAX_ROWS = 3;

/** Hours closer together than this draw as one bar. Consecutive archive files
 *  abut to within the flush's own jitter. */
const JOIN_GAP_MS = 60_000;

/** Colours go to the component as inline styles, so a custom property follows
 *  a theme change without the rows being rebuilt. */
export const ROW_COLOURS = { real: "var(--accent)", synthetic: "var(--text-muted)" };

export function mergeIntervals(files: ArchiveFile[]): [number, number][] {
  const out: [number, number][] = [];
  for (const f of files.slice().sort((a, b) => a.startMs - b.startMs)) {
    const last = out[out.length - 1];
    if (last && f.startMs <= last[1] + JOIN_GAP_MS) last[1] = Math.max(last[1], f.endMs);
    else out.push([f.startMs, f.endMs]);
  }
  return out;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * One row per node up to three, then one aggregate row, or two when the fleet
 * has a synthetic node at all: real and synthetic side by side is the
 * comparison worth making, and a split with nothing on one side is not.
 *
 * `anySynthetic` is asked of every known node rather than of `ids`, so the
 * colouring does not change meaning as the selection narrows.
 */
export function timelineRows(
  ids: string[],
  files: ArchiveFile[],
  synthetic: (id: string) => boolean,
  anySynthetic: boolean,
): TimelineRow[] {
  const colour = (id: string) =>
    anySynthetic && synthetic(id) ? ROW_COLOURS.synthetic : ROW_COLOURS.real;

  if (ids.length <= MAX_ROWS) {
    return ids.map((id) => ({
      id,
      title: id,
      color: colour(id),
      intervals: mergeIntervals(files.filter((f) => f.node === id)),
    }));
  }

  const within = (set: Set<string>) => mergeIntervals(files.filter((f) => set.has(f.node)));
  if (!anySynthetic) {
    return [
      {
        id: "all",
        title: plural(ids.length, "node"),
        color: ROW_COLOURS.real,
        intervals: within(new Set(ids)),
      },
    ];
  }

  const real = ids.filter((id) => !synthetic(id));
  const synth = ids.filter(synthetic);
  const rows: TimelineRow[] = [];
  if (real.length) {
    rows.push({
      id: "real",
      title: plural(real.length, "real node"),
      color: ROW_COLOURS.real,
      intervals: within(new Set(real)),
    });
  }
  if (synth.length) {
    rows.push({
      id: "synth",
      title: plural(synth.length, "synth node"),
      color: ROW_COLOURS.synthetic,
      intervals: within(new Set(synth)),
    });
  }
  return rows;
}

const allDay = (f: ExplorerFilters) => f.todFrom === TOD_START && f.todTo === TOD_END;

/** The filters as the timeline's highlighted range: whole days, or the exact
 *  times when a time of day is set. */
export function temporalRangeFor(f: ExplorerFilters): { start: number; end: number } {
  if (!allDay(f)) {
    return {
      start: Date.parse(`${f.from}T${f.todFrom}:00Z`),
      end: Date.parse(`${f.to}T${f.todTo}:00Z`),
    };
  }
  return { start: Date.parse(`${f.from}T00:00:00Z`), end: Date.parse(`${f.to}T00:00:00Z`) + DAY_MS };
}

/** The range as a line of text beside the card title. */
export function rangeLabel(f: ExplorerFilters): string {
  return `${f.from} → ${f.to}${allDay(f) ? "" : ` · ${f.todFrom}–${f.todTo}Z`}`;
}

/** Reset the dates and the time of day, and nothing else. */
export function clearRange(f: ExplorerFilters, today: string): ExplorerFilters {
  const d = defaultFilters(today);
  return { ...f, from: d.from, to: d.to, todFrom: TOD_START, todTo: TOD_END };
}

const dayOf = (ms: number) => isoDay(new Date(ms));
const clampDay = (day: string, today: string) => (day > today ? today : day);

/** A window ending on midnight ends at the last minute of the day before it,
 *  or the window would read as ending before it starts. */
const endOfWindow = (ms: number) => (hhmm(ms) === TOD_START ? TOD_END : hhmm(ms));

/**
 * A range dragged along the top strip. Anything within one day keeps its
 * times as a time-of-day window; anything longer is whole days. No range at
 * all is the component's "clear".
 */
export function filtersFromTemporal(
  f: ExplorerFilters,
  change: TimelineChange,
  today: string,
): ExplorerFilters {
  const { temporalStart: start, temporalEnd: end } = change;
  if (start == null || end == null) return clearRange(f, today);

  const from = clampDay(dayOf(start), today);
  let to = clampDay(dayOf(end - 1), today);
  if (to < from) to = from;

  const withinOneDay = end - start < DAY_MS && from === to;
  return {
    ...f,
    from,
    to,
    todFrom: withinOneDay ? hhmm(start) : TOD_START,
    todTo: withinOneDay ? endOfWindow(end) : TOD_END,
  };
}

/**
 * A date label clicked, which focuses the interval under it. A day focuses
 * that day; an hour focuses that hour of it. A month is too coarse to act on
 * and leaves the filters alone, which is why this can return null.
 */
export function filtersFromFocus(
  f: ExplorerFilters,
  change: TimelineChange,
  today: string,
): ExplorerFilters | null {
  const { focusedStart: start, focusedEnd: end } = change;
  if (start == null || end == null) return null;
  const span = end - start;
  if (span > DAY_MS * 1.5) return null;

  const day = clampDay(dayOf(start), today);
  const partial = span < DAY_MS * 0.9;
  return {
    ...f,
    from: day,
    to: day,
    todFrom: partial ? hhmm(start) : TOD_START,
    todTo: partial ? endOfWindow(end) : TOD_END,
  };
}
