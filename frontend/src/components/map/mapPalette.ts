/**
 * Every colour the map draws with, in one place.
 *
 * These were spread as hex literals across the icon factories, the legend, the
 * stats panel, the detail panel and half a dozen Leaflet layers. Several of
 * them have to agree — a lane's colour appears on the plane icon, in the
 * legend, on the stats row that counts it and on the arc drawn under it — and
 * they only agree if they read the same constant. They did not always: the
 * "dark aircraft" grey was `#94a3b8` on the Physics tab's icon and `#64748b` on
 * its own legend two elements away.
 *
 * Values are unchanged from the ones they replace. This is a move, not a
 * redesign; anything that looks different after it is a mistake.
 */

/* ── Track lanes ──────────────────────────────────────────────────────────
   Four position sources, four colours. The two blues sit next to each other
   because both lanes know the transponder identity; violet is the odd one out
   because a dark solve does not. */

/** Multi-node solve that carried a transponder tag (mn-adsb-*). */
export const LANE_MN_ADSB = "#38bdf8"; // sky-400
/** Multi-node solve with no transponder identity (mn-dark-*). */
export const LANE_MN_DARK = "#a78bfa"; // violet-400
/** Single node claiming a target off its real ADS-B fix. */
export const LANE_ADSB_SINGLE = "#3b82f6"; // blue-500
/** Solver run seeded from an ADS-B position. */
export const LANE_SOLVER_SEED = "#2dd4bf"; // teal-400

/* ── Map furniture ───────────────────────────────────────────────────────── */

/** Node markers, their uncertainty disc, and range rings. Amber rather than
 *  red so they do not share a palette with the anomalous-aircraft marker: the
 *  two reds were close enough that anomalies were mistaken for static nodes. */
export const NODE = "#facc15"; // yellow-400
/** Broadcast illuminators. */
export const ILLUMINATOR = "#f472b6"; // pink-400
/** Empirical coverage polygons. Green here means coverage, never truth. */
export const COVERAGE = "#22c55e"; // green-500
/** The selection highlight: selected track, its arcs, its range rings. */
export const SELECTED = "#fbbf24"; // amber-400
/** Anomalous tracks, and the pulsing ring around them. */
export const ANOMALY = "#f43f5e"; // rose-500
/** Drones, which are not aircraft and should not be read as one. */
export const DRONE = "#f59e0b"; // amber-500
/** ADS-B ground truth: the reference dot, its trail, the error lines. */
export const TRUTH = "#22d3ee"; // cyan-400
/** The "dark aircraft" ground-truth dot: a simulated target flying without
 *  ADS-B. Grey, so a viewer can tell at a glance which truth dots the radar
 *  has to find on its own. */
export const TRUTH_DARK = "#94a3b8"; // slate-400
/** MLAT verification overlay. */
export const MLAT = "#e879f9"; // fuchsia-400

/* ── Neutrals for map-drawn geometry and text ────────────────────────────── */

/** Reads against every fill on the map — the selection ring on a truth dot. */
export const INK = "#f8fafc";
export const INK_MUTED = "#94a3b8";
export const INK_SUBTLE = "#64748b";

/* ── Quality scale ────────────────────────────────────────────────────────
   Good / attention / bad. Position error, solver confidence, detection age. */

export const GOOD = "#34d399"; // emerald-400
export const WARN = "#f59e0b"; // amber-500
export const BAD = "#f43f5e"; // rose-500

/* ── Simulation object classes ────────────────────────────────────────────
   The Physics tab's legend and the truth dots the fleet spawns. Aliases of the
   values above rather than new ones: docs/simulation.md promises the map and
   that legend agree, which only holds if they read the same constants. */

export const SIM_COMMERCIAL = LANE_MN_ADSB;
export const SIM_DARK = TRUTH_DARK;
export const SIM_DRONE = DRONE;
export const SIM_ANOMALOUS = ANOMALY;
/** The fleet-scene controls, which reshape the whole world rather than one
 *  object class, and are deliberately not one of the class colours. */
export const SIM_SCENE = LANE_MN_DARK;

/* ── Altitude bands ───────────────────────────────────────────────────────
   Low warm → high cool. Band edges are multiples of 5000 ft so they line up
   with the AircraftMarker altBand memo key. */

export const ALT_BANDS: [number, string, string][] = [
  [40000, "#a855f7", "40k+"], // purple-500
  [30000, "#3b82f6", "30–40k"], // blue-500
  [20000, "#22c55e", "20–30k"], // green-500
  [10000, "#eab308", "10–20k"], // yellow-500
  [5000, "#f97316", "5–10k"], // orange-500
  [0, "#ef4444", "<5k"], // red-500
];

/** Doppler ramp for the bistatic arcs: dark blue (approaching) through the
 *  centre to dark red (receding). t ∈ [-1, +1] maps linearly across the five
 *  stops; dopplerColor in constants.ts does the interpolation. */
export const DOPPLER_STOPS: [number, number, number][] = [
  [0x1e, 0x3a, 0x8a], // -1.0  dark blue
  [0x60, 0xa5, 0xfa], // -0.5  light blue
  [0x22, 0xd3, 0xee], //  0.0  cyan
  [0xf8, 0x71, 0x71], // +0.5  light red
  [0x99, 0x1b, 0x1b], // +1.0  dark red
];
