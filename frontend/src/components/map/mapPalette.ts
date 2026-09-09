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
   Four position sources, four colours. All four draw the SAME plane glyph, so
   colour is the only thing telling them apart and they are chosen to be as far
   apart as the surface allows: no lane pair is closer than CIEDE2000 22.8,
   against 14.8 for the first light-surface pass, which read as a wall of
   near-identical blues at icon size.

   Two blues still sit next to each other because both those lanes know the
   transponder identity, but cyan and blue rather than two shades of one hue —
   the tighter family reading was not worth the legibility.

   The ceiling here is the basemap. Every value has to clear 3:1 against
   Positron's near-white land fill, which rules out the light, vivid end of
   every hue and leaves eight categories competing for the dark end. */

/** Multi-node solve that carried a transponder tag (mn-adsb-*). */
export const LANE_MN_ADSB = "#0891b2"; // cyan-600
/** Multi-node solve with no transponder identity (mn-dark-*). */
export const LANE_MN_DARK = "#a21caf"; // fuchsia-700
/** Single node claiming a target off its real ADS-B fix. */
export const LANE_ADSB_SINGLE = "#2563eb"; // blue-600
/** Solver run seeded from an ADS-B position. */
export const LANE_SOLVER_SEED = "#047857"; // emerald-700

/* ── Map furniture ───────────────────────────────────────────────────────── */

/** Node markers, their uncertainty disc, and range rings. A step darker than
 *  the obvious amber: yellow-600 measured 2.62:1 on Positron, so the receivers
 *  were the one thing on the map you could not reliably see. */
export const NODE = "#a16207"; // yellow-700
/** Broadcast illuminators. */
export const ILLUMINATOR = "#9d174d"; // pink-800
/** Empirical coverage polygons. Green here means coverage, never truth. */
export const COVERAGE = "#16a34a"; // green-600
/** The selection highlight: selected track, its arcs, its range rings. */
export const SELECTED = "#d97706"; // amber-600
/** Anomalous tracks, and the pulsing ring around them. */
export const ANOMALY = "#dc2626"; // red-600
/** Drones, which are not aircraft and should not be read as one. */
export const DRONE = "#ea580c"; // orange-600
/** ADS-B ground truth: the reference dot, its trail, the error lines.
 *
 *  Neutral rather than chromatic, and deliberately so. Truth is what the
 *  solved lanes are measured against, not a fifth lane, and any colour it
 *  borrowed landed within CIEDE2000 11 of the cyan lane — the single worst
 *  confusion on the old light map. A near-black dot beside a coloured plane
 *  reads as "here is where it actually is". */
export const TRUTH = "#1e293b"; // slate-800
/** The "dark aircraft" ground-truth dot: a simulated target flying without
 *  ADS-B. Grey, and light enough to stay clear of TRUTH beside it. */
export const TRUTH_DARK = "#64748b"; // slate-500
/** MLAT verification overlay. */
export const MLAT = "#c026d3"; // fuchsia-600

/* ── Neutrals for map-drawn text and inert geometry ───────────────────────
   These match the chrome's ink ramp (dash's --text-*) so a label drawn onto
   the map reads as the same system as a label drawn in a panel. */

export const INK = "#0f172a";
export const INK_MUTED = "#475569";
export const INK_SUBTLE = "#94a3b8";

/** The surface accent, for SVG drawn inline. Matches `--accent-hover`, which
 *  a presentation attribute cannot read: `var()` resolves in CSS, not in a
 *  bare `stroke="…"`. */
export const ACCENT_STRONG = "#2563eb";

/* ── Quality scale ────────────────────────────────────────────────────────
   Good / attention / bad, at dash's semantic values darkened for light tiles.
   Used for position error, solver confidence and detection age. */

export const GOOD = "#059669"; // emerald-600
export const WARN = "#d97706"; // amber-600
export const BAD = "#e11d48"; // rose-600

/* ── Simulation object classes ────────────────────────────────────────────
   The Physics tab's legend, and the truth dots the fleet spawns for each
   class. Aliases rather than new values: docs/simulation.md promises the map
   and that legend agree ("ADS-B truth dots are blue and dark aircraft grey"),
   which only holds if they read the same constants. */

export const SIM_COMMERCIAL = LANE_MN_ADSB;
export const SIM_DARK = INK_MUTED;
export const SIM_DRONE = DRONE;
export const SIM_ANOMALOUS = ANOMALY;
/** The fleet-scene controls, which reshape the whole world rather than one
 *  object class, and are deliberately not one of the class colours. */
export const SIM_SCENE = LANE_MN_DARK;

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
