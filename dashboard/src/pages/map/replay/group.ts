/* ------------------------------------------------------------------ */
/*  Grouping /api/data/tracks rows into tracks.                       */
/*                                                                    */
/*  A row names its track by track_key. Files written before that     */
/*  column existed fall back to the ADS-B hex, and a dark legacy row  */
/*  is linked to the nearest recent track of its lane. A linked       */
/*  track keeps the key the window before gave it, so its hex holds   */
/*  from one window to the next.                                      */
/* ------------------------------------------------------------------ */

import { distanceKm } from "../../../utils/geo";
import type { TrackRow } from "./types";

/** The multinode key prefix of an ADS-B-keyed track (backend MN_ADSB_PREFIX). */
export const ADSB_KEY_PREFIX = "mn-adsb-";
/** A legacy dark row links to a track whose last row is at most this much older... */
export const LINK_MAX_GAP_MS = 60_000;
/** ...and at most this far away (great circle). */
export const LINK_MAX_DISTANCE_M = 3_000;

export interface GroupedTrack {
  /** The track's multinode key: its track_key, `mn-adsb-<hex>`, or one minted by linking (here, or in the window before). */
  key: string;
  /** True when linking created the track, which keeps it open to later legacy rows. */
  linked: boolean;
  /** Indices into the rows passed in, in processing order (frame_ts_ms, then solve_ts_ms). */
  rows: number[];
}

/**
 * The rows of one grouping's linked tracks, each with its track's key: what
 * the next window's grouping reads to keep those keys (groupTrackRows'
 * `previous`). A row is known by its frame_ts_ms, lat and lon, exactly as the
 * API sent them. Column per field, sorted by frame_ts_ms: 28 bytes a row.
 */
export interface MintedRows {
  readonly frameTs: Float64Array;
  readonly lat: Float64Array;
  readonly lon: Float64Array;
  /** Each row's track, as an index into `keys`. */
  readonly track: Int32Array;
  readonly keys: readonly string[];
}

const NO_MINTED_ROWS: MintedRows = {
  frameTs: new Float64Array(0),
  lat: new Float64Array(0),
  lon: new Float64Array(0),
  track: new Int32Array(0),
  keys: [],
};

/** The rows of `tracks`' linked tracks, under the keys the tracks ended up with. */
export function mintedRowsOf(tracks: readonly GroupedTrack[], rows: readonly TrackRow[]): MintedRows {
  let n = 0;
  for (const t of tracks) if (t.linked) n += t.rows.length;
  if (n === 0) return NO_MINTED_ROWS;

  const keys: string[] = [];
  const at = new Int32Array(n);
  const owner = new Int32Array(n);
  let m = 0;
  for (const t of tracks) {
    if (!t.linked) continue;
    const k = keys.push(t.key) - 1;
    for (const i of t.rows) {
      at[m] = i;
      owner[m++] = k;
    }
  }
  const order = new Int32Array(n);
  for (let j = 0; j < n; j++) order[j] = j;
  order.sort((a, b) => rows[at[a]].frame_ts_ms - rows[at[b]].frame_ts_ms);

  const frameTs = new Float64Array(n);
  const lat = new Float64Array(n);
  const lon = new Float64Array(n);
  const track = new Int32Array(n);
  for (let j = 0; j < n; j++) {
    const row = rows[at[order[j]]];
    frameTs[j] = row.frame_ts_ms;
    lat[j] = row.lat;
    lon[j] = row.lon;
    track[j] = owner[order[j]];
  }
  return { frameTs, lat, lon, track, keys };
}

/** The key of the linked track that held a row identical to `row` (frame_ts_ms, lat, lon), if one did. */
function mintedKeyOf(minted: MintedRows, row: TrackRow): string | undefined {
  const { frameTs } = minted;
  const ts = row.frame_ts_ms;
  let lo = 0;
  let hi = frameTs.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (frameTs[mid] < ts) lo = mid + 1;
    else hi = mid;
  }
  for (let j = lo; j < frameTs.length && frameTs[j] === ts; j++) {
    if (minted.lat[j] === row.lat && minted.lon[j] === row.lon) return minted.keys[minted.track[j]];
  }
  return undefined;
}

/**
 * Rule 4: a linked track that holds a row a linked track of `previous` held
 * takes that track's key. Its earliest such row (in processing order) names
 * the key it asks for, and of the tracks asking for one key the one whose row
 * comes first gets it; the others keep the key they minted. A key another track
 * of this grouping already holds is never taken, so no two tracks share one.
 */
function carryKeys(tracks: GroupedTrack[], rows: readonly TrackRow[], order: readonly number[], previous: MintedRows): void {
  const position = new Int32Array(rows.length);
  for (let p = 0; p < order.length; p++) position[order[p]] = p;

  const claims = new Map<string, { track: GroupedTrack; at: number }>();
  for (const track of tracks) {
    if (!track.linked) continue;
    // A track's rows are in processing order, so the first one found is its earliest.
    for (const i of track.rows) {
      const key = mintedKeyOf(previous, rows[i]);
      if (key === undefined) continue;
      const claim = claims.get(key);
      if (!claim || position[i] < claim.at) claims.set(key, { track, at: position[i] });
      break;
    }
  }
  if (claims.size === 0) return;

  const held = new Set(tracks.map((t) => t.key));
  for (const [key, { track }] of claims) {
    if (key === track.key || !held.has(key)) track.key = key;
  }
}

interface LinkHead {
  track: GroupedTrack;
  /** The lane that created the track: the only lane it links in. */
  lane: string;
  /** The track's last row. */
  frameTs: number;
  lat: number;
  lon: number;
  /** Whether it is in its lane's list of heads. */
  listed: boolean;
}

// A latitude difference alone puts a lower bound on the great-circle distance
// (distanceKm's sphere), so a candidate further apart in latitude than the link
// radius is skipped before the haversine. The margin keeps a pair exactly at
// the radius to the exact test.
const LINK_MAX_DLAT_DEG = ((LINK_MAX_DISTANCE_M / 6_371_000) * (180 / Math.PI)) * (1 + 1e-9);

function usable(row: TrackRow): boolean {
  return (
    Number.isFinite(row.frame_ts_ms) &&
    Number.isFinite(row.solve_ts_ms) &&
    Number.isFinite(row.lat) &&
    Number.isFinite(row.lon)
  );
}

/**
 * The usable rows' indices in processing order: frame_ts_ms ascending, ties by
 * solve_ts_ms, then as given.
 */
function processingOrder(rows: readonly TrackRow[]): number[] {
  const order: number[] = [];
  for (let i = 0; i < rows.length; i++) if (usable(rows[i])) order.push(i);
  const before = (i: number, j: number) =>
    rows[i].frame_ts_ms - rows[j].frame_ts_ms || rows[i].solve_ts_ms - rows[j].solve_ts_ms;
  // The API sorts its rows, so this is a check, not a step. Sorting is stable,
  // which keeps the response order for rows equal on both keys.
  for (let k = 1; k < order.length; k++) {
    if (before(order[k - 1], order[k]) > 0) {
      order.sort(before);
      break;
    }
  }
  return order;
}

/**
 * The legacy dark key minted for a linked track, in the backend's own dark-key
 * format. Python's f"{x:.3f}" and toFixed(3) both round the exact binary value;
 * they part only on an exact binary tie (x.0625 and the like), where Python
 * rounds half to even and toFixed away from zero. The key never leaves the
 * client, so the two need not agree there.
 */
export function mintedDarkKey(row: Pick<TrackRow, "frame_ts_ms" | "lat" | "lon">): string {
  return `mn-dark-${row.frame_ts_ms}-${row.lat.toFixed(3)}-${row.lon.toFixed(3)}`;
}

/**
 * Groups rows into tracks, processing them in response order (frame_ts_ms,
 * then solve_ts_ms):
 *
 * 1. A row with a track_key belongs to that key's track.
 * 2. Otherwise a row with an adsb_hex belongs to `mn-adsb-<adsb_hex>`.
 * 3. Otherwise the row links to a track created by linking, of the same lane
 *    (a null lane counts as dark), whose last row is strictly earlier, at most
 *    LINK_MAX_GAP_MS earlier and at most LINK_MAX_DISTANCE_M away: the nearest
 *    one, and of equally near ones the one whose last row is most recent. With
 *    no candidate it starts a track under a minted dark key.
 * 4. A track rule 3 started keeps the key of the window before, given as
 *    `previous` (the minted rows of its grouping), when it holds a row that
 *    window's linked track held (see carryKeys). A window minted from its own
 *    first row would otherwise give a track that runs across windows a new hex
 *    at every window change.
 *
 * Tracks keyed by the server are never link candidates, and never change key.
 * One key is one track whichever rule produced it, so a minted key that is
 * already in use joins that track rather than emitting a second entry under
 * the same hex.
 *
 * Rows without a finite frame_ts_ms, solve_ts_ms, lat or lon are left out: the
 * contract has none, and one would put NaN into the linking and the trails.
 */
export function groupTrackRows(rows: readonly TrackRow[], previous?: MintedRows | null): GroupedTrack[] {
  const tracks: GroupedTrack[] = [];
  const byKey = new Map<string, GroupedTrack>();
  // Per lane, the linked tracks whose last row is recent enough to take
  // another. Rows arrive in frame order, so a head that falls out of the gap
  // never comes back into it, and dropping it keeps each scan short.
  const heads = new Map<string, LinkHead[]>();
  const headOf = new Map<GroupedTrack, LinkHead>();

  const open = (key: string, linked: boolean): GroupedTrack => {
    const track: GroupedTrack = { key, linked, rows: [] };
    tracks.push(track);
    byKey.set(key, track);
    return track;
  };

  // A row added to a linked track becomes its last row, whichever rule put it there.
  const append = (track: GroupedTrack, i: number, lane: string) => {
    track.rows.push(i);
    if (!track.linked) return;
    const row = rows[i];
    let head = headOf.get(track);
    if (!head) {
      head = { track, lane, frameTs: 0, lat: 0, lon: 0, listed: false };
      headOf.set(track, head);
    }
    head.frameTs = row.frame_ts_ms;
    head.lat = row.lat;
    head.lon = row.lon;
    if (!head.listed) {
      const list = heads.get(head.lane) ?? [];
      heads.set(head.lane, list);
      list.push(head);
      head.listed = true;
    }
  };

  const order = processingOrder(rows);
  for (const i of order) {
    const row = rows[i];

    const serverKey =
      row.track_key != null ? row.track_key : row.adsb_hex != null ? ADSB_KEY_PREFIX + row.adsb_hex : null;
    const lane = row.lane ?? "dark";
    if (serverKey != null) {
      append(byKey.get(serverKey) ?? open(serverKey, false), i, lane);
      continue;
    }

    const laneHeads = heads.get(lane) ?? [];
    heads.set(lane, laneHeads);

    let best: LinkHead | null = null;
    let bestM = Infinity;
    let kept = 0;
    for (const head of laneHeads) {
      if (row.frame_ts_ms - head.frameTs > LINK_MAX_GAP_MS) {
        head.listed = false; // out of reach for every later row too
        continue;
      }
      laneHeads[kept++] = head;
      if (head.frameTs >= row.frame_ts_ms) continue;
      if (Math.abs(head.lat - row.lat) > LINK_MAX_DLAT_DEG) continue;
      const m = distanceKm(head.lat, head.lon, row.lat, row.lon) * 1000;
      if (m > LINK_MAX_DISTANCE_M) continue;
      if (m < bestM || (m === bestM && head.frameTs > best.frameTs)) {
        best = head;
        bestM = m;
      }
    }
    laneHeads.length = kept;

    let track = best?.track;
    if (!track) {
      const key = mintedDarkKey(row);
      track = byKey.get(key) ?? open(key, true);
    }
    append(track, i, lane);
  }

  if (previous && previous.frameTs.length > 0) carryKeys(tracks, rows, order, previous);
  return tracks;
}
