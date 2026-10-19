/* ------------------------------------------------------------------ */
/*  Archive mode on the live map: which clock the map reads, and      */
/*  where an icon is drawn.                                           */
/*                                                                    */
/*  Live, the map runs on the wall clock and glides each icon ahead   */
/*  of its last fix. In archive replay every store is stamped with    */
/*  the replayed frame's clock, so everything that ages an entry      */
/*  reads that clock instead, and an icon sits exactly where the      */
/*  solve put it: the archive holds solves, not a path between them.  */
/* ------------------------------------------------------------------ */

import { ARC_DR_MAX_S, POSITION_SOURCE_ARC_ONLY } from "./constants";
import type { FeedMode } from "./feedMode";
import type { SolveTrailPoint } from "./trails";
import type { TrailPoint } from "./types";

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;

/** Longest a fix is dead-reckoned live, in seconds (arc-only tracks get ARC_DR_MAX_S). */
export const DR_MAX_S = 60;
const DEG_PER_M = 1 / 111_320;
const KNOTS_TO_MS = 0.514444;

/** The archive replays the real fleet; /sim stays live, and a `t` in its hash is ignored. */
export function archiveAvailable(mode: FeedMode): boolean {
  return mode !== "synthetic";
}

/** Whether the map replays the archive: an instant in the hash, on a page that has archive mode. */
export function isArchiveMode(mode: FeedMode, t: number | null | undefined): boolean {
  return archiveAvailable(mode) && t != null;
}

/**
 * The archive clock: the instant of the last frame the replay emitted, which is
 * what the map's stores hold, or the replay's own clock before the first one.
 * Null when the map is live.
 */
export function archiveClockMs(archive: boolean, frameNowMs: number | null, clockMs: number): number | null {
  if (!archive) return null;
  return frameNowMs ?? clockMs;
}

/** The map's clock: the archive clock when there is one, the wall clock otherwise. */
export function mapNowMs(archiveClock: number | null | undefined): number {
  return archiveClock ?? Date.now();
}

/** Where "Open archive" lands: an hour before `nowMs`, on the minute. */
export function archiveEntryMs(nowMs: number): number {
  return Math.floor((nowMs - HOUR_MS) / MINUTE_MS) * MINUTE_MS;
}

/**
 * Whether the first-data fit gives way to the view in the hash: only for an
 * archive link that names a centre, which is the place a sighting was reported.
 * A live link keeps the fit it always had.
 */
export function skipInitialFit(archive: boolean, hash: { lat?: number; lon?: number }): boolean {
  return archive && hash.lat != null && hash.lon != null;
}

/**
 * The timestamp, in seconds, of a point in the per-solve trail buffer: the
 * solve's own epoch in archive replay, when the entry carries one, and the
 * ingest time otherwise (live, where the feed sends no epoch).
 */
export function solveTrailTsS(ac: { frame_ts_ms?: unknown }, nowMs: number, archive: boolean): number {
  const epoch = ac.frame_ts_ms;
  if (archive && typeof epoch === "number" && Number.isFinite(epoch)) return epoch / 1000;
  return nowMs / 1000;
}

/**
 * A per-hex trail buffer in archive replay, rebuilt from the trail the
 * replayed entry carries.
 *
 * Every replayed frame carries each track's archived trail
 * (`recent_positions`: [lat, lon, alt_ft, ts_s] at the solve epochs,
 * ascending, none past the clock), so the buffer is that trail and not what
 * the frames since the link or the last seek happened to add: its last
 * `maxPoints` points as [lat, lon, ts_s]. `sigmaM`, when a positive number, is
 * the entry's own pos_sigma_m and goes on the last point alone, the solve the
 * entry describes. The archive keeps no sigma for the earlier points, which
 * the smoother's jump gate then judges at its fallback.
 *
 * The tuples are new: the frame's are shared with the replay index and with
 * every other frame that shows the same row.
 */
export function archiveTrailBuffer(
  positions: readonly TrailPoint[] | null | undefined,
  maxPoints: number,
  sigmaM?: unknown,
): SolveTrailPoint[] {
  if (!Array.isArray(positions)) return [];
  const out: SolveTrailPoint[] = [];
  for (let i = Math.max(0, positions.length - maxPoints); i < positions.length; i++) {
    const [lat, lon, , tsS] = positions[i];
    out.push([lat, lon, tsS]);
  }
  if (out.length && typeof sigmaM === "number" && sigmaM > 0) out[out.length - 1][3] = sigmaM;
  return out;
}

/** A drawn icon: position and heading. */
export interface Pose {
  lat: number;
  lon: number;
  track: number;
}

/** The fields of a stored fix the display rule reads. */
export interface PoseFix {
  _fixLat: number;
  _fixLon: number;
  _fixTs: number;
  gs?: number;
  track?: number;
  position_source?: string;
}

/**
 * Where an icon is drawn on this animation frame, written into `out` and
 * returned. The render loop passes its smoothing entry as `out`, and `prev`
 * may be that same object: it is read in full before `out` is written, so the
 * loop allocates nothing per frame.
 *
 * Live: the fix is dead-reckoned along its ground speed and track for the time
 * since it last moved (at most DR_MAX_S, or ARC_DR_MAX_S for an arc-only
 * track), and the previous pose eases toward that target by `alpha`.
 *
 * Archive: the fix itself, heading as the feed stated it, however long ago the
 * solve was. Replay shows what was published at the time and no more.
 */
export function displayPose(
  fix: PoseFix,
  prev: Pose | undefined,
  nowMs: number,
  alpha: number,
  archive: boolean,
  out: Pose,
): Pose {
  if (archive) {
    out.lat = fix._fixLat;
    out.lon = fix._fixLon;
    out.track = fix.track || 0;
    return out;
  }

  const drCapS = fix.position_source === POSITION_SOURCE_ARC_ONLY ? ARC_DR_MAX_S : DR_MAX_S;
  const elapsed = Math.min((nowMs - fix._fixTs) / 1000, drCapS);
  const gs = fix.gs || 0;

  // 1. Dead-reckon the physics target.
  let targetLat = fix._fixLat;
  let targetLon = fix._fixLon;
  if (elapsed > 0 && gs > 0) {
    const gsMs = gs * KNOTS_TO_MS;
    const trackRad = (fix.track || 0) * (Math.PI / 180);
    const cosLat = Math.cos(fix._fixLat * (Math.PI / 180)) || 1e-9;
    targetLat = fix._fixLat + gsMs * Math.cos(trackRad) * DEG_PER_M * elapsed;
    targetLon = fix._fixLon + (gsMs * Math.sin(trackRad)) / (111_320 * cosLat) * elapsed;
  }

  // 2. Exponential smoothing toward the target (the glide).
  const sLat = prev ? prev.lat + (targetLat - prev.lat) * alpha : targetLat;
  const sLon = prev ? prev.lon + (targetLon - prev.lon) * alpha : targetLon;

  // Heading, with wrap-around.
  const targetTrack = fix.track || 0;
  const prevTrack = prev ? prev.track : targetTrack;
  const dTrack = ((targetTrack - prevTrack + 540) % 360) - 180;
  const sTrack = (prevTrack + dTrack * alpha + 360) % 360;

  out.lat = sLat;
  out.lon = sLon;
  out.track = sTrack;
  return out;
}
