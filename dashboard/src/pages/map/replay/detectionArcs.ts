/* ------------------------------------------------------------------ */
/*  Archive replay: the detections of one /api/data/detections window */
/*  held by column, and the detection_arcs a frame carries for them.  */
/*                                                                    */
/*  buildDetectionWindow reads an answer once, when it lands; then    */
/*  detectionArcs turns the rows a frame shows into the entries the   */
/*  map's afterglow buffer takes, each drawn around the receiver and  */
/*  transmitter the archive holds for it.                             */
/* ------------------------------------------------------------------ */

import { buildBistaticArc, type NodeGeometry } from "../bistaticArc";
import { sha256Hex } from "./hex";
import type { DetectionsResponse, ReplayAircraft, ReplayArc } from "./types";

/**
 * One answer's rows, by column, sorted by frame time. A row's receiver,
 * transmitter and node are a site: `site[i]` indexes the sites, -1 when the
 * row lacks any of the four coordinates. `tag[i]` indexes `tags`, the
 * lower-cased ADS-B hexes, -1 for an untagged row. NaN stands for a null.
 */
export interface DetectionWindow {
  /** The window asked for, [from, to). */
  readonly from: number;
  readonly to: number;
  /**
   * The end of what it serves, exclusive: `to`, or for an answer the server
   * cut, the last row's frame time (from + 1 when that is `from`), since rows
   * at that instant may have been cut. Rows from there on are not held.
   */
  readonly servedEnd: number;
  readonly truncated: boolean;
  readonly local: boolean;
  readonly rowCount: number;

  readonly frameTs: Float64Array;
  readonly delayUs: Float64Array;
  readonly dopplerHz: Float64Array;
  readonly site: Int32Array;
  readonly tag: Int32Array;

  readonly tags: readonly string[];
  readonly siteNode: readonly string[];
  readonly siteRxLat: Float64Array;
  readonly siteRxLon: Float64Array;
  readonly siteTxLat: Float64Array;
  readonly siteTxLon: Float64Array;
  /** The passive hex of the site's node (passiveHex). */
  readonly sitePassiveHex: readonly string[];
}

/**
 * The hex an untagged detection is keyed by: "pr" and ten hex digits of the
 * node ref's SHA-256. One per node, never an ICAO or a multinode ("mn")
 * hex, and of the family the live feed's pending arcs use.
 */
export function passiveHex(nodeRef: string): string {
  return `pr${sha256Hex(nodeRef).slice(0, 10)}`;
}

/** An ADS-B hex as the live feed keys it: trimmed and lower-cased; "" for none. */
function normalHex(hex: unknown): string {
  return typeof hex === "string" ? hex.trim().toLowerCase() : "";
}

function numberOr(x: unknown): number {
  return typeof x === "number" && Number.isFinite(x) ? x : NaN;
}

/**
 * Reads one answer for the window [from, to) it was asked for. Throws when
 * the body cannot be read: not an object, rows not a list, or a row without
 * a node_ref or a finite frame_ts_ms. Rows outside the window are left out.
 */
export function buildDetectionWindow(resp: DetectionsResponse, from: number, to: number): DetectionWindow {
  if (typeof resp !== "object" || resp === null || !Array.isArray(resp.rows)) {
    throw new TypeError("not a detections answer");
  }
  const rows = resp.rows;
  for (const row of rows) {
    if (typeof row !== "object" || row === null || typeof row.node_ref !== "string" || !row.node_ref) {
      throw new TypeError("a detection row without a node_ref");
    }
    if (typeof row.frame_ts_ms !== "number" || !Number.isFinite(row.frame_ts_ms)) {
      throw new TypeError("a detection row without a frame time");
    }
  }

  // The route sorts by frame time; an answer that is not sorted is sorted here.
  let order: number[] | null = null;
  for (let i = 1; i < rows.length; i++) {
    if (rows[i].frame_ts_ms < rows[i - 1].frame_ts_ms) {
      order = rows.map((_, j) => j).sort((a, b) => rows[a].frame_ts_ms - rows[b].frame_ts_ms || a - b);
      break;
    }
  }
  const at = (k: number) => rows[order ? order[k] : k];

  let last = -Infinity;
  for (const row of rows) {
    const ts = row.frame_ts_ms;
    if (ts >= from && ts < to && ts > last) last = ts;
  }
  const truncated = resp.truncated === true;
  const servedEnd = truncated && last > -Infinity ? (last > from ? last : from + 1) : to;

  let n = 0;
  for (const row of rows) if (row.frame_ts_ms >= from && row.frame_ts_ms < servedEnd) n++;

  const frameTs = new Float64Array(n);
  const delayUs = new Float64Array(n);
  const dopplerHz = new Float64Array(n);
  const site = new Int32Array(n);
  const tag = new Int32Array(n);

  const tagIds = new Map<string, number>();
  const tags: string[] = [];
  const siteIds = new Map<string, number>();
  const siteNode: string[] = [];
  const coords: number[] = [];

  let k = 0;
  for (let j = 0; j < rows.length; j++) {
    const row = at(j);
    const ts = row.frame_ts_ms;
    if (ts < from || ts >= servedEnd) continue;
    frameTs[k] = ts;
    delayUs[k] = numberOr(row.delay_us);
    dopplerHz[k] = numberOr(row.doppler_hz);

    const rxLat = numberOr(row.rx_lat);
    const rxLon = numberOr(row.rx_lon);
    const txLat = numberOr(row.tx_lat);
    const txLon = numberOr(row.tx_lon);
    if (Number.isNaN(rxLat) || Number.isNaN(rxLon) || Number.isNaN(txLat) || Number.isNaN(txLon)) {
      site[k] = -1;
    } else {
      const key = `${row.node_ref}|${rxLat}|${rxLon}|${txLat}|${txLon}`;
      let id = siteIds.get(key);
      if (id === undefined) {
        id = siteNode.length;
        siteIds.set(key, id);
        siteNode.push(row.node_ref);
        coords.push(rxLat, rxLon, txLat, txLon);
      }
      site[k] = id;
    }

    const hex = normalHex(row.adsb_hex);
    if (!hex) {
      tag[k] = -1;
    } else {
      let id = tagIds.get(hex);
      if (id === undefined) {
        id = tags.length;
        tagIds.set(hex, id);
        tags.push(hex);
      }
      tag[k] = id;
    }
    k++;
  }

  const sites = siteNode.length;
  const column = (offset: number) => Float64Array.from({ length: sites }, (_, s) => coords[s * 4 + offset]);
  const passive = new Map<string, string>();
  const sitePassiveHex = siteNode.map((ref) => {
    let hex = passive.get(ref);
    if (hex === undefined) {
      hex = passiveHex(ref);
      passive.set(ref, hex);
    }
    return hex;
  });

  return {
    from,
    to,
    servedEnd,
    truncated,
    local: resp.local !== false,
    rowCount: n,
    frameTs,
    delayUs,
    dopplerHz,
    site,
    tag,
    tags,
    siteNode,
    siteRxLat: column(0),
    siteRxLon: column(1),
    siteTxLat: column(2),
    siteTxLon: column(3),
    sitePassiveHex,
  };
}

/** The first row of `w` with a frame time after `t`. */
function firstAfter(w: DetectionWindow, t: number): number {
  let lo = 0;
  let hi = w.rowCount;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (w.frameTs[mid] <= t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * The locus of one detection: built from the receiver and transmitter the
 * archive holds for it, never the registry's, which say where the node is
 * now. The registry gives only what the archive does not hold, the node's
 * beam and range; a node it does not list gets the builder's defaults.
 *
 * Built afresh for every frame that hands the detection over, a step back
 * included. An arc takes well under a microsecond to build, and keeping the
 * ones built would cost every tick of playback, which never shows one twice,
 * more than it saves a step back (detectionArcsBudget.test.ts prints both).
 */
function arcOfSite(
  w: DetectionWindow,
  s: number,
  delayUs: number,
  nodesByRef: Record<string, NodeGeometry> | null | undefined,
): [number, number][] | null {
  const node = nodesByRef?.[w.siteNode[s]];
  const geometry: NodeGeometry = {
    rx_lat: w.siteRxLat[s],
    rx_lon: w.siteRxLon[s],
    tx_lat: w.siteTxLat[s],
    tx_lon: w.siteTxLon[s],
  };
  if (node) {
    geometry.beam_azimuth_deg = node.beam_azimuth_deg;
    geometry.beam_width_deg = node.beam_width_deg;
    geometry.max_range_km = node.max_range_km;
    geometry.max_bistatic_range_km = node.max_bistatic_range_km;
  }
  return buildBistaticArc(delayUs, geometry);
}

/** The frame's aircraft by ADS-B hex: of several with one hex, the latest solve, the first in frame order on a tie. */
function aircraftByAdsbHex(aircraft: readonly ReplayAircraft[]): Map<string, ReplayAircraft> {
  const byHex = new Map<string, ReplayAircraft>();
  for (const ac of aircraft) {
    const hex = normalHex(ac.adsb_hex);
    if (!hex) continue;
    const held = byHex.get(hex);
    if (!held || ac.frame_ts_ms > held.frame_ts_ms) byHex.set(hex, ac);
  }
  return byHex;
}

/** Rows of `window` after `lo`: the part of it one frame still has to show. */
export interface ArcSource {
  window: DetectionWindow;
  lo: number;
}

/**
 * The detection_arcs of a frame at `hi`: one entry per row of each source
 * with lo < frame_ts_ms <= hi, in frame-time order within a source.
 *
 * - A row with no delay above 0, a coordinate missing, or no locus of at
 *   least two points draws nothing.
 * - Its hex: a tagged row belongs to the frame's aircraft with that ADS-B
 *   hex, the replayed track, when there is one, and takes its altitude;
 *   otherwise it keeps its own hex, as the live feed keys it. An untagged
 *   row takes its node's passive hex.
 * - `ts` is its frame time, which the map fades it from.
 *
 * Nothing about the aircraft changes: replay never adds one the tracks
 * store does not hold.
 */
export function detectionArcs(
  sources: readonly ArcSource[],
  hi: number,
  aircraft: readonly ReplayAircraft[],
  nodesByRef: Record<string, NodeGeometry> | null | undefined,
): ReplayArc[] {
  const out: ReplayArc[] = [];
  let byHex: Map<string, ReplayAircraft> | null = null;
  for (const { window: w, lo } of sources) {
    for (let i = firstAfter(w, lo); i < w.rowCount; i++) {
      const ts = w.frameTs[i];
      if (ts > hi) break;
      const delayUs = w.delayUs[i];
      const s = w.site[i];
      if (!(delayUs > 0) || s < 0) continue;
      const arc = arcOfSite(w, s, delayUs, nodesByRef);
      if (!arc || arc.length < 2) continue;

      let hex: string;
      let altBaro: number | null = null;
      const t = w.tag[i];
      if (t < 0) {
        hex = w.sitePassiveHex[s];
      } else {
        byHex ??= aircraftByAdsbHex(aircraft);
        const ac = byHex.get(w.tags[t]);
        if (ac) {
          hex = ac.hex;
          altBaro = ac.alt_baro ?? null;
        } else {
          hex = w.tags[t];
        }
      }
      const dopplerHz = w.dopplerHz[i];
      out.push({
        hex,
        node_ref: w.siteNode[s],
        ambiguity_arc: arc,
        delay_us: delayUs,
        doppler_hz: Number.isNaN(dopplerHz) ? 0 : dopplerHz,
        alt_baro: altBaro,
        ts,
      });
    }
  }
  return out;
}
