/* ------------------------------------------------------------------ */
/*  Refresh policy for the selected track's per-solve history           */
/* ------------------------------------------------------------------ */

/**
 * When LiveAircraftMap should re-read `/api/test/mlat-history` for the
 * multi-node aircraft the operator has selected.  That payload draws
 * MlatSolveHistoryLayer's per-solve dots and the detail panel's solve table —
 * the surface someone selects an aircraft in order to read, so it lagging the
 * marker is the whole bug.
 *
 * Two rules, because a poll alone cannot be both cheap and prompt: a floor
 * interval that runs regardless, and an event that fires inside it.  No
 * Leaflet, no React — unit-tested on its own.
 */

/**
 * Poll interval for the selected track's solve history, in ms.
 *
 * It was 30 s, sized when the solver refused to re-solve the same tracks
 * inside SOLVER_RESOLVE_INTERVAL_S = 12 s, so a poll could not miss much.
 * Dark solves now land every 1–3 s while a track is held, and at 30 s the dots
 * trailed the live marker by up to half a minute — a decomposition of the
 * track that was mostly missing.  3 s tracks that cadence at ~20 requests a
 * minute for ONE selected track, and only while something is selected.
 */
export const MLAT_HISTORY_REFRESH_MS = 3_000;

/** The two fields that decide a refetch: which track, and how old its last
 *  solve was when the feed last said. */
export interface SelectedSolveAge {
  /** Feed hex of the selected multi-node entry, or null when none is. */
  hex: string | null;
  /** `seen` from that entry — the backend's age of its last solve, seconds. */
  seen: number | null;
}

/**
 * True when the feed has just announced a fresh solve for the SAME selected
 * track, so its history is worth refetching ahead of the next poll.
 *
 * `seen` is an age, so it climbs on every flush and only ever falls when a new
 * solve replaced the one it was measuring — that fall is the event.  A change
 * of `hex` is not a comparison at all but a different clock, and the selection
 * effect refetches from scratch anyway; a null on either side is a track that
 * is not a multi-node solve, or a feed entry that never carried an age.
 */
export function newSolveArrived(prev: SelectedSolveAge, next: SelectedSolveAge): boolean {
  if (prev.hex !== next.hex || next.hex == null) return false;
  if (prev.seen == null || next.seen == null) return false;
  return next.seen < prev.seen;
}
