export const API_BASE = "/api";
export const STALE_AIRCRAFT_MS = 8000;
export const MAX_HISTORY = 150;
export const VIEWPORT_PAD_DEG = 1.5;

// Arc fade lifecycle.  Single source of truth for the renderer (LiveAircraftMap
// DetectionArcs) and the buffer pruner (hooks.useAircraftFeed).  They must
// agree: a renderer life > pruner TTL would leave the renderer hunting for
// already-deleted buffer entries; the reverse keeps stale entries in memory.
export const ARC_HOLD_MS = 0;
export const ARC_FADE_MS = 5_000;
export const ARC_TOTAL_LIFE_MS = ARC_HOLD_MS + ARC_FADE_MS;

// Ground-truth objects and radar tracks both live in the frontend's per-hex
// animation stores (fixesRef / smoothRef / trail buffers).  In simulation a
// radar track carries the *same* ICAO hex as the aircraft it came from, so
// without a namespace the two write to one key and the marker alternates
// between the solved position and the true one on every ingest — measured at
// 29.8 km apart for a single-node arc track on staging.  Truth entries are
// therefore stored under this prefix; `hex` on the object stays the real hex
// so labels, selection and error computation are unaffected.
// TTL for truth objects when the feed stops (pushes arrive every ~2 s).
export const GT_FEED_STALE_MS = 30_000;
// Grace before a truth object missing from a snapshot is forgotten.  The
// backend can legitimately drop a hex from one snapshot and bring it back:
// the GT snapshot is rebuilt only every 5 s (GT_REFRESH_S) while the trail GC
// runs on a 10 s staleness rule, so a push hiccup or a single dropped WS
// frame produced a visible blink when the prune fired on first absence.
export const GT_PRUNE_GRACE_MS = 10_000;
export const GT_KEY_PREFIX = "gt:";
export const groundTruthKey = (hex) => GT_KEY_PREFIX + hex;

// position_source string for single-node arc-only aircraft (lat/lon is the
// arc midpoint, not a real fix).  Backend emits this verbatim — keep in sync
// with the backend constant if it ever moves.
export const POSITION_SOURCE_ARC_ONLY = "single_node_ellipse_arc";

// position_source for an aircraft claimed by exactly ONE node: lat/lon is the
// real ADS-B fix (not an estimate), and the entry carries that node's full
// bistatic locus in ambiguity_arc for the frontend to trim.  Backend emits
// this verbatim — keep in sync with the backend constant if it ever moves.
export const POSITION_SOURCE_ADSB_SINGLE = "adsb_single_node";

// Three lanes, three colours (getAircraftColor, StatsOverlay, the trimmed arc):
// this blue for a claimed single-node ADS-B target, cyan #38bdf8 for a
// multi-node solve that carried a transponder tag (mn-adsb-*, adsb_assisted),
// violet #a78bfa for a dark multi-node solve (mn-dark-*).  The two blues sit
// next to each other because both lanes know the transponder identity; violet
// is the odd one out because a dark solve does not.  Teal #2dd4bf stays on the
// ADS-B-seeded solver source, and cyan doubles as the fallback colour for the
// rare solver_single_node relic.
export const ADSB_SINGLE_COLOR = "#3b82f6";

// The claimed arc is drawn at a FIXED SCREEN LENGTH — a multiple of the plane
// icon it sits under — rather than a fixed ground length.  The locus spans
// tens of km, so any geographic length either vanishes at low zoom or swamps
// the map at high zoom; pinning it to the icon keeps it readable as "the
// locus runs this way through this target" at every zoom.  2.5 × the 18–30 px
// altitude bands (icons.ts) gives a 45–75 px section.
export const ADSB_SINGLE_ARC_ICON_MULTIPLE = 2.5;

// Dead-reckoning elapsed cap (seconds) for arc-only tracks.  Their backend
// position is pinned to the arc midpoint between delay updates, so a long
// glide walks the anchor straight off the measured locus: with the generic
// 60 s cap, staging measured 26/415 displayed arc-only positions outside
// their own node's beam wedge, median 5.9 km from their own arc (7–11 km
// after a full 60 s glide).  10 s keeps short gaps smooth while bounding the
// divergence.  Other position sources keep the 60 s cap.  (Arc-only tracks
// render no plane icon, but the DR position still drives arc-rebuild
// anchoring, list centering, and the smooth store.)
export const ARC_DR_MAX_S = 10;

// Dead-reckoning drift budget (metres) past which the plane ICON stops being
// drawn as an ordinary live target.  The backend keeps feeding an mn entry past
// its last solve (MN_DARK_EXPIRY_S 30 s dark, 60 s ADS-B-assisted), so a target
// whose solves stop is drawn kilometres from where it actually is — the icon
// reads as a real target because nothing about it looks stale.
//
// The budget is LANE-AWARE, because the two multi-node lanes re-solve at very
// different rates and one budget cannot describe both:
//
//  * Assisted lane (mn-adsb-*) and every other source — 2 km.  Measured
//    re-solve cadence is 2.9 s median, so a healthy target accrues a few
//    hundred metres and never trips it; at airliner speed the icon survives
//    ~6–8 s of solve loss.  2 km also matches the known-lane publish
//    displacement gate (_MAX_DISPLACEMENT_KM) and sits under backend dedup's
//    3 km proximity gate, so a second icon cannot appear at the true position
//    while the drifted one is still shown.
//  * Dark lane (mn-dark-*) — 3 km.  It was 6 km, sized when the solver refused
//    to re-solve the same tracks inside SOLVER_RESOLVE_INTERVAL_S = 12 s and
//    the measured dark cadence was 9.0 s median / 24.9 s p90: at that cadence
//    2 km hid 48% of published dark track-frames and only 6 km got 90% of them
//    drawn.  Dark solves now land every 1–3 s, so the drift a healthy track
//    accrues is metres, and the budget stopped buying coverage and started
//    buying wrong icons: measured against ground truth over 20 minutes, dark
//    entries run 1.50 km median error at 8–15 s of solve age but 2.02 km at
//    15–30 s (7% over 5 km) and 3.99 km at 30–60 s (32% over 5 km).  3 km sits
//    at that knee — it still tolerates the odd missed solve, and it no longer
//    draws a confident icon 6 km from any aircraft.
//
// Exceeding the budget does not mean the same thing in both lanes, so neither
// does the rendering (see drIconState in icons.ts): an assisted track over
// budget is a genuine anomaly and loses its icon, while a dark track over
// budget is the normal consequence of a missed solve and is drawn in a
// degraded "stale solve" style instead — "solved but stale" has to stay
// distinguishable from "not solved".  Either way the TRACK stays alive —
// stores, trails, list, selection — so a new solve restores the normal icon on
// the next 2 Hz render.
export const DR_ICON_HIDE_DISTANCE_M = 2000;
export const DR_ICON_HIDE_DISTANCE_DARK_M = 3000;

// Ground speed (knots) assumed when a multi-node entry carries no `gs` at all.
// The backend deletes gs from entries whose velocity vector it does not trust
// (aircraft_feed, VEL_TRUST_MODE=active) — precisely the entries whose
// dead-reckoned position deserves the least confidence.  Reading an absent gs
// as "0 kt, therefore no drift" inverted the gate: untrustworthy entries were
// the only ones that could never be hidden, while trustworthy fast ones were.
// 250 kt is a deliberately middling airliner cruise figure: high enough that a
// long solve gap trips the budget, low enough that it does not hide a track
// after a couple of seconds on an assumption the feed never made.
export const DR_UNKNOWN_GS_KT = 250;

// Doppler colour gradient — dark blue (approaching) → light blue → cyan → light red → dark red (receding)
// Centre stop is bright cyan so near-zero-doppler arcs are always visible on light basemaps.
// t ∈ [-1, +1] maps linearly across the 5 stops.
const _DOPPLER_STOPS = [
  [0x1e, 0x3a, 0x8a], // -1.0  dark blue
  [0x60, 0xa5, 0xfa], // -0.5  light blue
  [0x22, 0xd3, 0xee], //  0.0  cyan-400 (replaces grey — visible on any basemap)
  [0xf8, 0x71, 0x71], // +0.5  light red
  [0x99, 0x1b, 0x1b], // +1.0  dark red
];
export function dopplerColor(doppler_hz, maxDop = 200) {
  const t = Math.max(-1, Math.min(1, doppler_hz / maxDop)); // [-1, +1]
  const pos = (t + 1) / 2 * (_DOPPLER_STOPS.length - 1);   // [0, 4]
  const lo = Math.floor(pos);
  const hi = Math.min(lo + 1, _DOPPLER_STOPS.length - 1);
  const f = pos - lo;
  const [r, g, b] = _DOPPLER_STOPS[lo].map((c, i) => Math.round(c + f * (_DOPPLER_STOPS[hi][i] - c)));
  return `#${r.toString(16).padStart(2, '0')}${g.toString(16).padStart(2, '0')}${b.toString(16).padStart(2, '0')}`;
}
