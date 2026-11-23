/* ------------------------------------------------------------------ */
/*  Archive replay: the snapshots of one /api/data/adsb window held   */
/*  by column, and the ground truth a frame carries for them: the     */
/*  ADS-B traffic the server knew of at the frame's instant.          */
/*                                                                    */
/*  buildAdsbWindow reads an answer once, when it lands; adsbTraffic  */
/*  then reads the few snapshots before the clock on every tick.      */
/*                                                                    */
/*  A snapshot holds the freshest fix the server had of an aircraft,  */
/*  which can be up to 45 s old, and repeats it while nothing newer   */
/*  comes in. So a trail point sits at the time its fix was received, */
/*  not at the snapshot's, and a fix two snapshots repeat is one      */
/*  point; the meta says when the shown fix was received.             */
/* ------------------------------------------------------------------ */

import { M_PER_FT, MS_PER_KNOT } from "../units";
import type { AdsbResponse, TruthMeta, TruthPoint } from "./types";

/** The server snapshots its ADS-B picture this often (ADSB_SNAPSHOT_INTERVAL_S). */
export const ADSB_SNAPSHOT_MS = 10_000;
/**
 * An aircraft shows while its latest snapshot at or before the clock is at
 * most this old: two snapshot periods, so one snapshot it is missing from
 * does not take it off the map, and two do. The snapshot's time decides, not
 * its fix's: ADSBHub traffic is polled in cycles, so its fixes are seconds old
 * whenever they are taken, and the panel says how old the shown one is.
 */
export const ADSB_FRESH_MS = 2 * ADSB_SNAPSHOT_MS;
/** A frame hands over at most this many points of an aircraft's trail, */
export const ADSB_TRAIL_POINTS = 30;
/** ... none of them more than this before its latest. */
export const ADSB_TRAIL_SPAN_MS = ADSB_TRAIL_POINTS * ADSB_SNAPSHOT_MS;
/** How far before the clock a frame reads: the oldest point of the oldest trail it can hand over. */
export const ADSB_LEAD_MS = ADSB_FRESH_MS + ADSB_TRAIL_SPAN_MS;

/**
 * One answer's rows, by column, in the route's order (snapshot time, then
 * hex), and the same rows again grouped by aircraft: aircraft h owns
 * byHex[hexStart[h]] .. byHex[hexStart[h + 1] - 1], ascending in time, and
 * row i sits at byHex[hexPos[i]]. NaN stands for a null.
 */
export interface AdsbWindow {
  /** The window asked for, [from, to). */
  readonly from: number;
  readonly to: number;
  /**
   * The end of what it serves, exclusive: `to`; or the archive's edge when it
   * landed, plus 1 ms, as rows flushed after that never reach it; or, for an
   * answer the server cut, its last row's snapshot (from + 1 when that is
   * `from`), whose rows may have been cut. Rows from there on are not held.
   */
  readonly servedEnd: number;
  readonly truncated: boolean;
  readonly rowCount: number;

  readonly ts: Float64Array;
  readonly hex: Int32Array;
  readonly speedMs: Float64Array;
  readonly heading: Float64Array;
  readonly flight: readonly (string | null)[];
  /**
   * When each row's fix was received, epoch ms: its seen_ms, never after its
   * snapshot, or the snapshot's own time for a row without one.
   */
  readonly received: Float64Array;
  /** Each row's trail point, at `received`, built once: shared by every frame that hands it over. */
  readonly points: readonly TruthPoint[];

  readonly hexes: readonly string[];
  readonly hexIds: ReadonlyMap<string, number>;
  readonly hexStart: Int32Array;
  readonly byHex: Int32Array;
  readonly hexPos: Int32Array;
}

function finite(x: unknown): number {
  return typeof x === "number" && Number.isFinite(x) ? x : NaN;
}

/**
 * Reads one answer for the window [from, to) it was asked for, which lands
 * with the archive's edge (the latest instant it holds in full) at `edge`.
 * Throws when the body cannot be read: not an object, rows not a list, or a
 * row without a hex or a finite ts_ms. Rows outside the window are left
 * out, and so is a row without a position, or a second row of one aircraft
 * in one snapshot.
 */
export function buildAdsbWindow(resp: AdsbResponse, from: number, to: number, edge = Infinity): AdsbWindow {
  if (typeof resp !== "object" || resp === null || !Array.isArray(resp.rows)) {
    throw new TypeError("not an ADS-B answer");
  }
  const rows = resp.rows;
  for (const row of rows) {
    if (typeof row !== "object" || row === null || typeof row.hex !== "string" || !row.hex.trim()) {
      throw new TypeError("an ADS-B row without a hex");
    }
    if (typeof row.ts_ms !== "number" || !Number.isFinite(row.ts_ms)) {
      throw new TypeError("an ADS-B row without a snapshot time");
    }
  }

  // The route sorts by snapshot time; an answer that is not sorted is sorted here.
  let order: number[] | null = null;
  for (let i = 1; i < rows.length; i++) {
    if (rows[i].ts_ms < rows[i - 1].ts_ms) {
      order = rows.map((_, j) => j).sort((a, b) => rows[a].ts_ms - rows[b].ts_ms || a - b);
      break;
    }
  }
  const at = (k: number) => rows[order ? order[k] : k];

  let last = -Infinity;
  for (const row of rows) {
    const ts = row.ts_ms;
    if (ts >= from && ts < to && ts > last) last = ts;
  }
  const truncated = resp.truncated === true;
  let servedEnd = Math.min(to, edge + 1);
  if (truncated && last > -Infinity) servedEnd = Math.min(servedEnd, last > from ? last : from + 1);

  const hexIds = new Map<string, number>();
  const hexes: string[] = [];
  const lastTs: number[] = [];
  const kept: number[] = [];
  const hexOf: number[] = [];
  for (let j = 0; j < rows.length; j++) {
    const row = at(j);
    const ts = row.ts_ms;
    if (ts < from || ts >= servedEnd) continue;
    if (Number.isNaN(finite(row.lat)) || Number.isNaN(finite(row.lon))) continue;
    const key = row.hex.trim().toLowerCase();
    let h = hexIds.get(key);
    if (h === undefined) {
      h = hexes.length;
      hexIds.set(key, h);
      hexes.push(key);
      lastTs.push(-Infinity);
    }
    // One fix per aircraft per snapshot, as the route sends them.
    if (lastTs[h] === ts) continue;
    lastTs[h] = ts;
    kept.push(order ? order[j] : j);
    hexOf.push(h);
  }

  const n = kept.length;
  const ts = new Float64Array(n);
  const hex = Int32Array.from(hexOf);
  const speedMs = new Float64Array(n);
  const heading = new Float64Array(n);
  const flight: (string | null)[] = new Array(n);
  const received = new Float64Array(n);
  const points: TruthPoint[] = new Array(n);
  for (let i = 0; i < n; i++) {
    const row = rows[kept[i]];
    ts[i] = row.ts_ms;
    const gs = finite(row.gs);
    speedMs[i] = gs * MS_PER_KNOT;
    heading[i] = finite(row.track);
    flight[i] = typeof row.flight === "string" && row.flight ? row.flight : null;
    // A row is shown only from its snapshot on, so a fix time no later than
    // the snapshot is never after the clock it is shown at.
    const seen = finite(row.seen_ms);
    received[i] = Number.isNaN(seen) ? row.ts_ms : Math.min(seen, row.ts_ms);
    const altFt = finite(row.alt_baro);
    points[i] = [row.lat, row.lon, Number.isNaN(altFt) ? null : altFt * M_PER_FT, received[i] / 1000];
  }

  // Grouped by aircraft: a counting sort on the hex, which keeps the time order.
  const hexStart = new Int32Array(hexes.length + 1);
  for (let i = 0; i < n; i++) hexStart[hex[i] + 1]++;
  for (let h = 0; h < hexes.length; h++) hexStart[h + 1] += hexStart[h];
  const fill = hexStart.slice(0, hexes.length);
  const byHex = new Int32Array(n);
  const hexPos = new Int32Array(n);
  for (let i = 0; i < n; i++) {
    const p = fill[hex[i]]++;
    byHex[p] = i;
    hexPos[i] = p;
  }

  return {
    from,
    to,
    servedEnd,
    truncated,
    rowCount: n,
    ts,
    hex,
    speedMs,
    heading,
    flight,
    received,
    points,
    hexes,
    hexIds,
    hexStart,
    byHex,
    hexPos,
  };
}

/** The first row of `w` with a snapshot after `t`, or at it too when `inclusive`. */
function firstFrom(w: AdsbWindow, t: number, inclusive: boolean): number {
  let lo = 0;
  let hi = w.rowCount;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (w.ts[mid] < t || (!inclusive && w.ts[mid] === t)) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** What a frame hands over for one aircraft, for the row it shows. */
export interface TrafficEntry {
  readonly window: AdsbWindow;
  readonly row: number;
  readonly trail: TruthPoint[];
  readonly meta: TruthMeta;
}

/**
 * The entries last handed over, by hex. An aircraft whose latest row is the
 * same hands over the same trail and meta, so the map can tell nothing moved.
 */
export type TrafficCache = Map<string, TrafficEntry>;

export interface Traffic {
  ground_truth: Record<string, TruthPoint[]>;
  ground_truth_meta: Record<string, TruthMeta>;
}

/**
 * The trail of row `row` of windows[k]: the fixes of the aircraft's rows up
 * to and including it, each once, oldest first, at most ADSB_TRAIL_POINTS and
 * from no snapshot ADSB_TRAIL_SPAN_MS or more before it, reaching back into
 * the windows before it. A row whose fix is no older than the point after it
 * (the same fix again, or one received later) adds nothing, so fix times
 * strictly increase and the last point is the row's own fix.
 */
function trailOf(windows: readonly AdsbWindow[], k: number, row: number): TruthPoint[] {
  const newest = windows[k].ts[row];
  const out: TruthPoint[] = [];
  const hexKey = windows[k].hexes[windows[k].hex[row]];
  let next = Infinity;
  for (let j = k; j >= 0 && out.length < ADSB_TRAIL_POINTS; j--) {
    const w = windows[j];
    const h = j === k ? w.hex[row] : w.hexIds.get(hexKey);
    if (h === undefined) continue;
    const first = w.hexStart[h];
    let p = j === k ? w.hexPos[row] : w.hexStart[h + 1] - 1;
    for (; p >= first && out.length < ADSB_TRAIL_POINTS; p--) {
      const i = w.byHex[p];
      if (w.ts[i] <= newest - ADSB_TRAIL_SPAN_MS) return out.reverse();
      if (w.received[i] >= next) continue;
      next = w.received[i];
      out.push(w.points[i]);
    }
  }
  return out.reverse();
}

function metaOf(w: AdsbWindow, row: number): TruthMeta {
  const meta: TruthMeta = {
    has_adsb: true,
    adsb_callsign: w.flight[row],
    object_type: "aircraft",
    source: "adsb",
    seen_ms: w.received[row],
  };
  if (!Number.isNaN(w.speedMs[row])) meta.speed_ms = w.speedMs[row];
  if (!Number.isNaN(w.heading[row])) meta.heading = w.heading[row];
  return meta;
}

/**
 * The ground truth of a frame at `clock`, from `windows`, which run on from
 * one another in time order: every aircraft whose latest row at or before
 * the clock is at most ADSB_FRESH_MS old, with that row's trail (trailOf)
 * and what it says of the aircraft, when its fix was received among it.
 * Nothing past the clock is read, so no position from after the instant ever
 * shows, and no point is stamped after it.
 *
 * `cache` holds the entries this handed over last; an aircraft whose latest
 * row has not changed hands over the same arrays again. Read them, never
 * edit them in place.
 */
export function adsbTraffic(windows: readonly AdsbWindow[], clock: number, cache?: TrafficCache): Traffic {
  const groundTruth: Record<string, TruthPoint[]> = {};
  const meta: Record<string, TruthMeta> = {};
  const shown = new Set<string>();
  const oldest = clock - ADSB_FRESH_MS;
  // Newest first, so the first row met of an aircraft is its latest.
  for (let k = windows.length - 1; k >= 0; k--) {
    const w = windows[k];
    if (w.rowCount === 0 || w.ts[0] > clock || w.ts[w.rowCount - 1] < oldest) continue;
    const stop = firstFrom(w, oldest, true);
    for (let i = firstFrom(w, clock, false) - 1; i >= stop; i--) {
      const hex = w.hexes[w.hex[i]];
      if (shown.has(hex)) continue;
      shown.add(hex);
      let entry = cache?.get(hex);
      if (!entry || entry.window !== w || entry.row !== i) {
        entry = { window: w, row: i, trail: trailOf(windows, k, i), meta: metaOf(w, i) };
        cache?.set(hex, entry);
      }
      groundTruth[hex] = entry.trail;
      meta[hex] = entry.meta;
    }
  }
  return { ground_truth: groundTruth, ground_truth_meta: meta };
}
