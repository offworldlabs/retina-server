/* ------------------------------------------------------------------ */
/*  Archive replay: one /api/data/tracks window turned into the feed  */
/*  frames the live map would have received, at any instant.          */
/*                                                                    */
/*  buildReplayIndex does every per-row step once, when a window      */
/*  loads; synthesizeFrame is then a binary search and a handful of   */
/*  field reads per track, cheap enough to run on every tick.         */
/* ------------------------------------------------------------------ */

import { MAX_TRAIL_POINTS } from "../trails";
import { ADSB_KEY_PREFIX, groupTrackRows, mintedRowsOf, type MintedRows } from "./group";
import { multinodeHexFromKey } from "./hex";
import type { ReplayAircraft, ReplayFrame, TrackRow, TracksResponse } from "./types";

/** A dark or follow track's entry expires this long after its solve epoch (backend MN_DARK_EXPIRY_S). */
export const DARK_EXPIRY_MS = 30_000;
/** An ADS-B-keyed track's entry expires this long after its solve epoch (backend MN_ASSISTED_EXPIRY_S). */
export const ADSB_EXPIRY_MS = 60_000;
/** A trail restarts after a gap longer than this between its points (backend TRAIL_STALE_S). */
export const TRAIL_STALE_MS = 300_000;
/** A row within this many degrees of the trail's last point in both lat and lon adds no point (append_track_history). */
export const TRAIL_DEDUP_DEG = 0.00005;

const M_PER_FT = 0.3048;
const KT_PER_MS = 1.94384;
const RAD_TO_DEG = 180 / Math.PI;

export interface ReplayIndex {
  readonly from: number;
  readonly to: number;
  readonly truncated: boolean;
  readonly rowCount: number;
  readonly trackCount: number;
  /**
   * The latest instant any row was published at, max(solve_ts_ms,
   * frame_ts_ms); -Infinity with no rows. A truncated window holds the rows up
   * to the server's cut, so it shows nothing it can vouch for past this.
   */
  readonly lastPublishedAt: number;
  /**
   * The latest frame_ts_ms of any row; -Infinity with no rows. The server cuts
   * a response after rows sorted by frame_ts_ms, so a truncated window can lack
   * rows with a later frame time than this, and past it a track's older
   * position may stand for one the cut left out.
   */
  readonly lastFrameTs: number;
}

type TrailPoint = [number, number, number, number];

/**
 * Everything a frame needs, laid out by column. Tracks are sorted by hex, and
 * track t owns rows trackStart[t] .. trackStart[t + 1] - 1, in publication
 * order. NaN stands for a null the entry omits.
 */
interface IndexData extends ReplayIndex {
  readonly hexes: string[];
  readonly adsbHexes: (string | undefined)[];
  readonly expiryMs: Float64Array;
  readonly trackStart: Int32Array;

  readonly publishedAt: Float64Array;
  readonly frameTs: Float64Array;
  readonly solveTs: Float64Array;
  readonly lat: Float64Array;
  readonly lon: Float64Array;
  readonly altFt: Float64Array;
  readonly gs: Float64Array;
  readonly heading: Float64Array;
  readonly nNodes: Int32Array;
  readonly refs: Int32Array;
  readonly rmsDelay: Float64Array;
  readonly rmsDoppler: Float64Array;
  readonly anomalous: Uint8Array;
  readonly anomalyTypes: Int32Array;
  readonly maxVelocity: Float64Array;
  readonly sigma: Float64Array;
  readonly trailFrom: Int32Array;
  readonly trailTo: Int32Array;

  /** Distinct node_refs lists and sorted anomaly_types lists, referenced by index. */
  readonly refLists: readonly string[][];
  readonly anomalyLists: readonly string[][];
  readonly flights: readonly string[];
  /** Every track's trail points, each track's contiguous, ascending in time. */
  readonly points: TrailPoint[];
  /** The linked tracks' rows and keys, which the next window's index carries on. */
  readonly minted: MintedRows;
}

/**
 * `x` rounded to `digits` decimals as Python's round() does it.
 *
 * Both round the exact binary value, so they differ only on an exact binary
 * tie (34.015625 to five places, say), where Python rounds half to even and
 * toFixed half away from zero. Math.round(x * 1e5) / 1e5 is not used: the
 * scaling itself lands on .5, and that disagreed with Python on about 1% of
 * real coordinates.
 */
export function roundTo(x: number, digits: number): number {
  return Number(x.toFixed(digits));
}

function nullable(x: number | null | undefined, digits: number): number {
  return x == null ? NaN : roundTo(x, digits);
}

function interner() {
  const ids = new Map<string, number>();
  const lists: string[][] = [];
  return {
    lists,
    id(list: readonly string[]): number {
      const key = list.join("\n");
      let id = ids.get(key);
      if (id === undefined) {
        id = lists.length;
        // Frozen: one list is shared by every entry that carries it.
        lists.push(Object.freeze(list.slice()) as string[]);
        ids.set(key, id);
      }
      return id;
    },
  };
}

interface TrackPlan {
  key: string;
  hex: string;
  rows: number[];
}

/**
 * Indexes one window. The rows are not kept: what the frames need is copied
 * into typed columns, and the response can be dropped once this returns.
 *
 * `previous` is the index of the window in use before this one. A track
 * linked from legacy rows keeps the key, and so the hex, that window gave it
 * when the two share a row (groupTrackRows, rule 4); without it, or with no
 * row in common, the track's key is minted from its first row here.
 */
export function buildReplayIndex(resp: TracksResponse, previous?: ReplayIndex | null): ReplayIndex {
  const rows: readonly TrackRow[] = resp.rows ?? [];
  const published = (r: TrackRow) => Math.max(r.solve_ts_ms, r.frame_ts_ms);

  const groups = groupTrackRows(rows, (previous as IndexData | null | undefined)?.minted ?? null);
  // Before the plans re-order each group's rows; the lookup sorts its own.
  const minted = mintedRowsOf(groups, rows);
  const plans: TrackPlan[] = groups.map((g) => ({
    key: g.key,
    hex: multinodeHexFromKey(g.key),
    // A row exists once the server has published it, and the server cannot
    // publish a solve before its own epoch. The order is stable, so rows equal
    // on both keys keep their processing order.
    rows: g.rows.sort((i, j) => {
      const a = rows[i];
      const b = rows[j];
      return published(a) - published(b) || a.frame_ts_ms - b.frame_ts_ms;
    }),
  }));
  plans.sort((a, b) => (a.hex < b.hex ? -1 : a.hex > b.hex ? 1 : 0));

  const trackCount = plans.length;
  const n = plans.reduce((sum, p) => sum + p.rows.length, 0);

  const hexes: string[] = new Array(trackCount);
  const adsbHexes: (string | undefined)[] = new Array(trackCount);
  const expiryMs = new Float64Array(trackCount);
  const trackStart = new Int32Array(trackCount + 1);

  const publishedAt = new Float64Array(n);
  const frameTs = new Float64Array(n);
  const solveTs = new Float64Array(n);
  const lat = new Float64Array(n);
  const lon = new Float64Array(n);
  const altFt = new Float64Array(n);
  const gs = new Float64Array(n);
  const heading = new Float64Array(n);
  const nNodes = new Int32Array(n);
  const refs = new Int32Array(n);
  const rmsDelay = new Float64Array(n);
  const rmsDoppler = new Float64Array(n);
  const anomalous = new Uint8Array(n);
  const anomalyTypes = new Int32Array(n);
  const maxVelocity = new Float64Array(n);
  const sigma = new Float64Array(n);
  const trailFrom = new Int32Array(n);
  const trailTo = new Int32Array(n);

  const refLists = interner();
  const anomalyLists = interner();
  const flights: string[] = [];
  const points: TrailPoint[] = [];
  let lastPublishedAt = -Infinity;
  let maxFrameTs = -Infinity;

  let k = 0;
  for (let t = 0; t < trackCount; t++) {
    const plan = plans[t];
    const assisted = plan.key.startsWith(ADSB_KEY_PREFIX);
    hexes[t] = plan.hex;
    adsbHexes[t] = assisted ? plan.key.slice(ADSB_KEY_PREFIX.length) : undefined;
    expiryMs[t] = assisted ? ADSB_EXPIRY_MS : DARK_EXPIRY_MS;
    trackStart[t] = k;

    // The fastest solve so far, as max_velocity_ms latches it; NaN until a row has a velocity.
    let maxSpeed = NaN;
    // The trail as the live feed's append_track_history would have built it
    // from this track's publishes, with each point stamped at its solve epoch.
    let last: TrailPoint | null = null;
    let lastFrameTs = 0;
    let segmentStart = points.length;

    for (const i of plan.rows) {
      const row = rows[i];
      publishedAt[k] = published(row);
      if (publishedAt[k] > lastPublishedAt) lastPublishedAt = publishedAt[k];
      frameTs[k] = row.frame_ts_ms;
      if (row.frame_ts_ms > maxFrameTs) maxFrameTs = row.frame_ts_ms;
      solveTs[k] = row.solve_ts_ms;
      lat[k] = roundTo(row.lat, 5);
      lon[k] = roundTo(row.lon, 5);
      const ft = row.alt_m == null ? NaN : Math.round(row.alt_m / M_PER_FT);
      altFt[k] = ft;

      const ve = row.vel_east_ms;
      const vn = row.vel_north_ms;
      if (ve == null || vn == null) {
        gs[k] = NaN;
        heading[k] = NaN;
      } else {
        // sqrt of the squares, not Math.hypot, which can differ from Python's
        // arithmetic in the last bit.
        const speed = Math.sqrt(ve * ve + vn * vn);
        gs[k] = roundTo(speed * KT_PER_MS, 1);
        // Python's float % keeps the divisor's sign, so a heading is never
        // negative; + 0 turns a -0 into the 0 Python gives.
        const deg = (Math.atan2(ve, vn) * RAD_TO_DEG) % 360;
        heading[k] = roundTo(deg < 0 ? deg + 360 : deg + 0, 1);
        if (!(speed <= maxSpeed)) maxSpeed = speed;
      }
      maxVelocity[k] = Number.isNaN(maxSpeed) ? NaN : roundTo(maxSpeed, 1);

      nNodes[k] = row.n_nodes;
      flights[row.n_nodes] ??= `MN${row.n_nodes}N`;
      refs[k] = refLists.id(row.node_refs ?? []);
      rmsDelay[k] = nullable(row.rms_delay_us, 3);
      rmsDoppler[k] = nullable(row.rms_doppler_hz, 2);
      anomalous[k] = row.is_anomalous === true ? 1 : 0;
      anomalyTypes[k] = anomalyLists.id((row.anomaly_types ?? []).slice().sort());
      sigma[k] = nullable(row.pos_sigma_m, 1);

      // A row adds a point unless it lies within TRAIL_DEDUP_DEG of the last
      // one in both axes (append_track_history), or is not later than it: the
      // live trail only ever grows forward, and the map's own merge drops a
      // point that is not (trails.ts). After a gap over TRAIL_STALE_MS the
      // live trail has been collected (feed_gc), so the row starts a new one
      // wherever it lies.
      const ts = roundTo(row.frame_ts_ms / 1000, 1);
      const gap = row.frame_ts_ms - lastFrameTs;
      const restart = last !== null && gap > TRAIL_STALE_MS;
      if (
        last === null ||
        restart ||
        (ts > last[3] &&
          !(Math.abs(last[0] - row.lat) < TRAIL_DEDUP_DEG && Math.abs(last[1] - row.lon) < TRAIL_DEDUP_DEG))
      ) {
        if (restart) segmentStart = points.length;
        last = [roundTo(row.lat, 6), roundTo(row.lon, 6), Number.isNaN(ft) ? 0 : ft, ts];
        lastFrameTs = row.frame_ts_ms;
        points.push(last);
      }
      trailTo[k] = points.length;
      trailFrom[k] = Math.max(segmentStart, points.length - MAX_TRAIL_POINTS);
      k++;
    }
  }
  trackStart[trackCount] = k;

  const index: IndexData = {
    from: resp.from,
    to: resp.to,
    truncated: resp.truncated === true,
    rowCount: n,
    trackCount,
    lastPublishedAt,
    lastFrameTs: maxFrameTs,
    hexes,
    adsbHexes,
    expiryMs,
    trackStart,
    publishedAt,
    frameTs,
    solveTs,
    lat,
    lon,
    altFt,
    gs,
    heading,
    nNodes,
    refs,
    rmsDelay,
    rmsDoppler,
    anomalous,
    anomalyTypes,
    maxVelocity,
    sigma,
    trailFrom,
    trailTo,
    refLists: refLists.lists,
    anomalyLists: anomalyLists.lists,
    flights,
    points,
    minted,
  };
  return index;
}

/**
 * The frame the live feed would have delivered at `clockMs`, in the shape
 * /ws/aircraft/live publishes.
 *
 * Each track contributes its latest row published by `clockMs`, provided that
 * row's solve epoch is within the track's expiry of the clock. Pure: the same
 * index and clock give the same frame.
 */
export function synthesizeFrame(index: ReplayIndex, clockMs: number): ReplayFrame {
  const ix = index as IndexData;
  const { publishedAt, frameTs, trackStart, points } = ix;
  const aircraft: ReplayAircraft[] = [];
  const anomalyHexes: string[] = [];

  for (let t = 0; t < ix.trackCount; t++) {
    // The last row published by the clock: the first one past it, minus one.
    let lo = trackStart[t];
    let hi = trackStart[t + 1];
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (publishedAt[mid] <= clockMs) lo = mid + 1;
      else hi = mid;
    }
    const r = lo - 1;
    if (r < trackStart[t] || clockMs - frameTs[r] > ix.expiryMs[t]) continue;

    const hex = ix.hexes[t];
    const n = ix.nNodes[r];
    const entry: ReplayAircraft = {
      hex,
      type: "multinode_solve",
      flight: ix.flights[n],
      lat: ix.lat[r],
      lon: ix.lon[r],
      multinode: true,
      position_source: "multinode_solve",
      adsb_assisted: false,
      frame_ts_ms: frameTs[r],
      solve_ts_ms: ix.solveTs[r],
    };
    const ft = ix.altFt[r];
    if (!Number.isNaN(ft)) {
      entry.alt_baro = ft;
      entry.alt_geom = ft;
    }
    if (!Number.isNaN(ix.gs[r])) {
      entry.gs = ix.gs[r];
      entry.track = ix.heading[r];
    }
    entry.solve_lat = ix.lat[r];
    entry.solve_lon = ix.lon[r];
    entry.seen = roundTo(Math.max(0, (clockMs - frameTs[r]) / 1000), 1);
    entry.n_nodes = n;
    entry.contributing_node_refs = ix.refLists[ix.refs[r]];
    if (!Number.isNaN(ix.rmsDelay[r])) entry.rms_delay = ix.rmsDelay[r];
    if (!Number.isNaN(ix.rmsDoppler[r])) entry.rms_doppler = ix.rmsDoppler[r];
    const anomalous = ix.anomalous[r] === 1;
    entry.is_anomalous = anomalous;
    entry.anomaly_types = ix.anomalyLists[ix.anomalyTypes[r]];
    if (!Number.isNaN(ix.maxVelocity[r])) entry.max_velocity_ms = ix.maxVelocity[r];
    const adsbHex = ix.adsbHexes[t];
    if (adsbHex !== undefined) {
      entry.adsb_assisted = true;
      entry.adsb_hex = adsbHex;
    }
    if (!Number.isNaN(ix.sigma[r])) entry.pos_sigma_m = ix.sigma[r];
    entry.recent_positions = points.slice(ix.trailFrom[r], ix.trailTo[r]);

    aircraft.push(entry);
    if (anomalous) anomalyHexes.push(hex);
  }

  return {
    now: clockMs / 1000,
    aircraft,
    detection_arcs: [],
    detecting_nodes: {},
    anomaly_hexes: anomalyHexes,
    ground_truth: {},
    ground_truth_meta: {},
  };
}
