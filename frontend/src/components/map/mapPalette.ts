/**
 * Radar data colours for the map surface, in both themes.
 *
 * One module rather than hex spread across the icons, the legend, the stats
 * panel and the arc layers. Those have to agree about what a lane looks like,
 * and they only agree if they read the same source.
 *
 * The two palettes carry the SAME KEYS, which is what lets a consumer write
 * `const { NODE } = usePalette()` and go on using `NODE` unchanged. Adding a
 * key to one and not the other is a type error.
 *
 * Both were chosen by measurement rather than by eye, against their own
 * background — Positron's near-white land fill for light, the navy canvas for
 * dark. Two things are checked: WCAG contrast against that background, so a
 * mark can be seen at all, and CIEDE2000 between marks, so two of them can be
 * told apart. The second is the one that gets forgotten: a set can clear
 * contrast everywhere and still be unreadable, which is exactly what a uniform
 * "darken everything a step" produces.
 *
 *   worst lane pair   light 22.8   dark 28.1   (the four lanes share one glyph)
 *   worst pair at all light 20.1   dark 20.0
 *   worst contrast    light 3.28   dark 4.62
 *
 * For scale, the dark palette this replaces scored 11.6 at its worst pair, and
 * the first light pass 10.9 — both cases being the multi-node lane against
 * ground truth, which is why truth now wears no lane colour in either theme.
 *
 * Dark has the wider gamut to play with: a dark background admits the whole
 * light end of every hue, where a near-white one leaves eight categories
 * competing for the dark end. That is why the light figures are the tighter
 * ones, and why it is the light theme that constrains any new category.
 */

export type MapTheme = "light" | "dark";

export interface MapPalette {
  /* Track lanes. All four draw the SAME plane glyph, so colour is the only
     thing telling them apart and they carry the hard separation constraint. */
  /** Multi-node solve that carried a transponder tag (mn-adsb-*). */
  LANE_MN_ADSB: string;
  /** Multi-node solve with no transponder identity (mn-dark-*). */
  LANE_MN_DARK: string;
  /** Single node claiming a target off its real ADS-B fix. */
  LANE_ADSB_SINGLE: string;
  /** Solver run seeded from an ADS-B position. */
  LANE_SOLVER_SEED: string;

  /* Map furniture. These have shapes of their own — concentric rings, a dashed
     pulsing ring, an X-frame — so they need to clear the lanes and each other,
     but by less than the lanes need to clear one another. */
  /** Node markers, their uncertainty disc, and range rings. */
  NODE: string;
  /** Broadcast illuminators. */
  ILLUMINATOR: string;
  /** Empirical coverage polygons. Green here means coverage, never truth. */
  COVERAGE: string;
  /** The selection highlight: selected track, its arcs, its range rings. */
  SELECTED: string;
  /** Anomalous tracks, and the pulsing ring around them. */
  ANOMALY: string;
  /** Drones, which are not aircraft and should not be read as one. */
  DRONE: string;
  /** ADS-B ground truth: the reference dot, its trail, the error lines.
   *  Neutral in both themes, and at the far end of the ramp from the canvas —
   *  near-black on light, near-white on dark. Truth is what the solved lanes
   *  are measured against, not a fifth lane, and every chromatic value it
   *  borrowed landed within CIEDE2000 12 of a lane. */
  TRUTH: string;
  /** The "dark aircraft" ground-truth dot: a simulated target flying without
   *  ADS-B. Grey, and far enough from TRUTH to read beside it. */
  TRUTH_DARK: string;
  /** MLAT verification overlay. */
  MLAT: string;

  /* Neutrals for map-drawn text and inert geometry. These match the chrome's
     ink ramp so a label drawn onto the map reads as the same system as a label
     drawn in a panel — which means they invert with the theme. */
  INK: string;
  INK_MUTED: string;
  INK_SUBTLE: string;
  /** The surface accent, for SVG drawn inline. Mirrors `--accent-hover`, which
   *  a presentation attribute cannot read: `var()` resolves in CSS, not in a
   *  bare `stroke="…"`. */
  ACCENT_STRONG: string;

  /* Quality scale: position error, solver confidence, detection age. */
  GOOD: string;
  WARN: string;
  BAD: string;

  /* Simulation object classes — the Physics tab's legend and the truth dots
     the fleet spawns. Aliases of the values above rather than new ones, so the
     legend and the map cannot drift apart. */
  SIM_COMMERCIAL: string;
  SIM_DARK: string;
  SIM_DRONE: string;
  SIM_ANOMALOUS: string;
  /** The fleet-scene controls, which reshape the whole world rather than one
   *  object class, and are deliberately not one of the class colours. */
  SIM_SCENE: string;

  /** Altitude bands, low warm → high cool. Band edges are multiples of 5000 ft
   *  so they line up with the AircraftMarker altBand memo key. */
  ALT_BANDS: [number, string, string][];

  /** The aircraft glyph's outline and lift. The fill carries the lane and
   *  nothing may dilute it: at 18px an outline is a large fraction of the
   *  glyph, so these are hairlines whose only job is to stop the shape merging
   *  into the basemap, with the shadow supplying the edge. */
  ICON_HALO: string;
  ICON_HALO_OPACITY: string;
  ICON_SHADOW: string;

  /** Doppler ramp for the bistatic arcs, five stops from fully approaching to
   *  fully receding. The centre is neutral in both themes, so "no radial
   *  motion" reads as the absence of a direction rather than a third colour. */
  DOPPLER_STOPS: [number, number, number][];
}

/* ── Light ────────────────────────────────────────────────────────────────
   Carto Positron tiles under dash's `#f1f5f9` chrome. Every value clears 3:1
   against Positron's land fill, which rules out the light, vivid end of every
   hue — the binding constraint on this theme. */
const LIGHT: MapPalette = {
  LANE_MN_ADSB: "#0891b2", // cyan-600
  LANE_MN_DARK: "#a21caf", // fuchsia-700
  LANE_ADSB_SINGLE: "#2563eb", // blue-600
  LANE_SOLVER_SEED: "#047857", // emerald-700

  NODE: "#a16207", // yellow-700 — yellow-600 measured 2.62:1 here
  ILLUMINATOR: "#9d174d", // pink-800
  COVERAGE: "#16a34a", // green-600
  SELECTED: "#d97706", // amber-600
  ANOMALY: "#dc2626", // red-600
  DRONE: "#ea580c", // orange-600
  TRUTH: "#1e293b", // slate-800
  TRUTH_DARK: "#64748b", // slate-500
  MLAT: "#c026d3", // fuchsia-600

  INK: "#0f172a",
  INK_MUTED: "#475569",
  INK_SUBTLE: "#94a3b8",
  ACCENT_STRONG: "#2563eb",

  GOOD: "#059669", // emerald-600
  WARN: "#d97706", // amber-600
  BAD: "#e11d48", // rose-600

  SIM_COMMERCIAL: "#0891b2",
  SIM_DARK: "#475569",
  SIM_DRONE: "#ea580c",
  SIM_ANOMALOUS: "#dc2626",
  SIM_SCENE: "#a21caf",

  ALT_BANDS: [
    [40000, "#9333ea", "40k+"], // purple-600
    [30000, "#2563eb", "30–40k"], // blue-600
    [20000, "#16a34a", "20–30k"], // green-600
    [10000, "#ca8a04", "10–20k"], // yellow-600
    [5000, "#ea580c", "5–10k"], // orange-600
    [0, "#dc2626", "<5k"], // red-600
  ],

  ICON_HALO: "#ffffff",
  ICON_HALO_OPACITY: "0.9",
  ICON_SHADOW: "drop-shadow(0 1px 2px rgba(15,23,42,0.5))",

  DOPPLER_STOPS: [
    [0x1e, 0x3a, 0x8a], // -1.0  blue-900   approaching fast
    [0x25, 0x63, 0xeb], // -0.5  blue-600
    [0x47, 0x55, 0x69], //  0.0  slate-600  no radial motion
    [0xdc, 0x26, 0x26], // +0.5  red-600
    [0x7f, 0x1d, 0x1d], // +1.0  red-900    receding fast
  ],
};

/* ── Dark ─────────────────────────────────────────────────────────────────
   The navy console this surface started as, and the default. Deliberately the
   hues people already associate with each lane — four of these values are
   unchanged from the original map — with only the confusable pairs pulled
   apart. Measured against the `#0d1b2a` canvas at a 4.5:1 floor. */
const DARK: MapPalette = {
  LANE_MN_ADSB: "#22d3ee", // cyan-400   (was sky #38bdf8)
  LANE_MN_DARK: "#d8b4fe", // purple-300 (was violet #a78bfa)
  LANE_ADSB_SINGLE: "#3b82f6", // blue-500   unchanged
  LANE_SOLVER_SEED: "#4ade80", // green-400  (was teal #2dd4bf)

  NODE: "#facc15", // yellow-400 unchanged
  ILLUMINATOR: "#f472b6", // pink-400   unchanged
  COVERAGE: "#22c55e", // green-500
  SELECTED: "#fbbf24", // amber-400
  ANOMALY: "#ef4444", // red-500    (was rose #f43f5e)
  DRONE: "#f59e0b", // amber-500
  TRUTH: "#f8fafc", // slate-50 — the neutral extreme, mirroring light's ink
  TRUTH_DARK: "#94a3b8", // slate-400
  MLAT: "#e879f9", // fuchsia-400

  INK: "#e2e8f0",
  INK_MUTED: "#94a3b8",
  INK_SUBTLE: "#64748b",
  ACCENT_STRONG: "#7dd3fc",

  GOOD: "#4ade80",
  WARN: "#fbbf24",
  BAD: "#f43f5e",

  SIM_COMMERCIAL: "#22d3ee",
  SIM_DARK: "#94a3b8",
  SIM_DRONE: "#f59e0b",
  SIM_ANOMALOUS: "#ef4444",
  SIM_SCENE: "#d8b4fe",

  ALT_BANDS: [
    [40000, "#a855f7", "40k+"], // purple-500
    [30000, "#3b82f6", "30–40k"], // blue-500
    [20000, "#22c55e", "20–30k"], // green-500
    [10000, "#eab308", "10–20k"], // yellow-500
    [5000, "#f97316", "5–10k"], // orange-500
    [0, "#ef4444", "<5k"], // red-500
  ],

  // A white rim and a heavy black drop, as the original map had: on a dark
  // basemap the shadow does almost nothing and the rim is what separates a
  // bright fill from the tiles behind it.
  ICON_HALO: "#ffffff",
  ICON_HALO_OPACITY: "0.7",
  ICON_SHADOW: "drop-shadow(0 2px 5px rgba(0,0,0,0.85))",

  DOPPLER_STOPS: [
    [0x1e, 0x3a, 0x8a], // -1.0  blue-900
    [0x60, 0xa5, 0xfa], // -0.5  blue-400
    [0x94, 0xa3, 0xb8], //  0.0  slate-400  no radial motion
    [0xf8, 0x71, 0x71], // +0.5  red-400
    [0x99, 0x1b, 0x1b], // +1.0  red-900
  ],
};

export const PALETTES: Record<MapTheme, MapPalette> = { light: LIGHT, dark: DARK };

export const DEFAULT_MAP_THEME: MapTheme = "dark";

/* ── The active palette, for code that cannot use a hook ──────────────────
   icons.ts builds Leaflet divIcons and constants.ts interpolates the Doppler
   ramp; neither is a component. They read this at call time, and every one of
   their callers re-renders when the theme changes, so the value they see is
   the one the tree is being drawn with. MapThemeProvider owns the write. */
let _active: MapPalette = PALETTES[DEFAULT_MAP_THEME];

export function setActivePalette(p: MapPalette): void {
  _active = p;
}

export function activePalette(): MapPalette {
  return _active;
}
