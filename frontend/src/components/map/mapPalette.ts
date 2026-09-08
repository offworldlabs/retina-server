/**
 * Radar data colours for the map surface.
 *
 * One module rather than hex spread across the icons, the legend, the stats
 * panel and the arc layers. Those have to agree about what a lane looks like,
 * and they only agree if they read the same constant.
 *
 * Every value here is a step darker than the shade it replaces. The surface is
 * light now — Carto Positron tiles under dash's `#f1f5f9` chrome — and the old
 * navy-calibrated shades (teal `#2dd4bf`, sky `#38bdf8`, violet `#a78bfa`) land
 * near 1.5:1 against white tiles, which is a stroke you cannot follow. Hue per
 * role is unchanged, so the map still says the same things in the same colours.
 *
 * The Doppler ramp is the exception and lives in constants.ts: its stops were
 * already chosen to carry on a light basemap.
 */

/* ── Track lanes ──────────────────────────────────────────────────────────
   Four position sources, four colours. The two blues sit together because
   both lanes know the transponder identity; violet is the odd one out
   because a dark solve does not. See the lane note in constants.ts. */

/** Multi-node solve that carried a transponder tag (mn-adsb-*). */
export const LANE_MN_ADSB = "#0284c7"; // sky-600
/** Multi-node solve with no transponder identity (mn-dark-*). */
export const LANE_MN_DARK = "#7c3aed"; // violet-600
/** Single node claiming a target off its real ADS-B fix. */
export const LANE_ADSB_SINGLE = "#2563eb"; // blue-600
/** Solver run seeded from an ADS-B position. */
export const LANE_SOLVER_SEED = "#0d9488"; // teal-600

/* ── Map furniture ───────────────────────────────────────────────────────── */

/** Node markers, their uncertainty disc, and range rings. */
export const NODE = "#ca8a04"; // yellow-600
/** Broadcast illuminators. */
export const ILLUMINATOR = "#db2777"; // pink-600
/** Empirical coverage polygons. Green here means coverage, never truth. */
export const COVERAGE = "#16a34a"; // green-600
/** The selection highlight: selected track, its arcs, its range rings. */
export const SELECTED = "#d97706"; // amber-600
/** Anomalous tracks, and the pulsing ring around them. */
export const ANOMALY = "#e11d48"; // rose-600
/** Drones, which are not aircraft and should not be read as one. */
export const DRONE = "#ea580c"; // orange-600
/** ADS-B ground truth: the reference dot, its trail, the error lines. */
export const TRUTH = "#0891b2"; // cyan-600
/** MLAT verification overlay. */
export const MLAT = "#c026d3"; // fuchsia-600

/* ── Neutrals for map-drawn text and inert geometry ───────────────────────
   These match the chrome's ink ramp (dash's --text-*) so a label drawn onto
   the map reads as the same system as a label drawn in a panel. */

export const INK = "#0f172a";
export const INK_MUTED = "#475569";
export const INK_SUBTLE = "#94a3b8";

/* ── Quality scale ────────────────────────────────────────────────────────
   Good / attention / bad, at dash's semantic values darkened for light tiles.
   Used for position error, solver confidence and detection age. */

export const GOOD = "#059669"; // emerald-600
export const WARN = "#d97706"; // amber-600
export const BAD = "#e11d48"; // rose-600

/* ── Altitude bands ───────────────────────────────────────────────────────
   Low warm → high cool. Band edges are multiples of 5000 ft so they line up
   with the AircraftMarker altBand memo key. */

export const ALT_BANDS: [number, string, string][] = [
  [40000, "#9333ea", "40k+"], // purple-600
  [30000, "#2563eb", "30–40k"], // blue-600
  [20000, "#16a34a", "20–30k"], // green-600
  [10000, "#ca8a04", "10–20k"], // yellow-600
  [5000, "#ea580c", "5–10k"], // orange-600
  [0, "#dc2626", "<5k"], // red-600
];
