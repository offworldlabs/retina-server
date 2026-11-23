/* ------------------------------------------------------------------ */
/*  The ADS-B fixtures: cuts of real GET /api/data/adsb answers,      */
/*  captured in process (FastAPI's TestClient) from a store seeded    */
/*  with seed_tracks.py and seed_adsb.py (day 2026-10-18, rng seed    */
/*  20261018), the seed the tracks fixtures in scenarios.ts come      */
/*  from. Each file is one captured answer: from, to, fleet and       */
/*  truncated as captured, the rows of the chosen hexes in a short    */
/*  sub-window, count the rows kept, each row the server's bytes.     */
/*                                                                    */
/*  All fleet=real (the explorer's only fleet):                       */
/*    2000z-adsb.json                 [19:30:00Z, 20:30:00Z), the     */
/*        window replay asks for at 20:00:00Z. Rows of [19:52:00Z,    */
/*        20:04:00Z) for a1dda8 (track A1 of 2000z-known.json, a      */
/*        known-lane solve), a07caf (no solve anywhere in the tracks  */
/*        store), adbeaa (track A6, whose ADS-B rows stop for 60 s    */
/*        after 19:59:10Z, its last five all holding the fix received */
/*        at 19:58:26Z, its last solve before the gap) and addd76     */
/*        (traffic with no callsign). Every row carries seen_ms, the  */
/*        fix's receipt as served: whole seconds, never after ts_ms.  */
/*    2000z-adsb-truncated.json       the same window, the route's    */
/*        row cap patched to 200 in process: cut, kept whole. Its     */
/*        last snapshot is 19:30:10Z, of which it holds 52 rows.      */
/*    2000z-adsb-after-truncation.json [19:30:10Z, 19:31:10Z), the    */
/*        window replay asks for next (from the last snapshot, a      */
/*        minute long), whole. Rows of its first 10 s: the snapshot   */
/*        at the cut, every hex.                                      */
/* ------------------------------------------------------------------ */

import type { AdsbResponse } from "../types";
import traffic from "./2000z-adsb.json";
import truncated from "./2000z-adsb-truncated.json";
import afterTruncation from "./2000z-adsb-after-truncation.json";

const response = (json: unknown) => json as AdsbResponse;

export const ADSB = {
  /** A known-lane solve's aircraft, one with no solve, one that drops out for 60 s, one with no callsign; whole. */
  traffic: response(traffic),
  /** An answer the route cut at its row cap. */
  truncated: response(truncated),
  /** The whole answer for the window after the cut one. */
  afterTruncation: response(afterTruncation),
};

/** The instant replay asks for 2000z-adsb.json's window at. */
export const ADSB_CLOCK_MS = Date.UTC(2026, 9, 18, 20, 0, 0);

/** The aircraft of track A1 in 2000z-known.json, solved on the known lane. */
export const KNOWN_LANE_HEX = "a1dda8";
/** An aircraft the tracks store holds no solve of. */
export const NO_SOLVE_HEX = "a07caf";
/**
 * Track A6's aircraft: its rows stop after 19:59:10Z and come back at
 * 20:00:10Z, and its snapshots 19:58:30Z..19:59:10Z all hold the fix received
 * at 19:58:26Z.
 */
export const GAP_HEX = "adbeaa";
/** An aircraft whose rows carry no callsign. */
export const NO_CALLSIGN_HEX = "addd76";
