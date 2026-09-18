import type { MapPalette } from "./mapPalette";

/* ── Ground-truth object classes and their colours.
 *
 * A truth dot answers two questions at once: what kind of target is this
 * (anomalous / drone / a plain aircraft), and — for a plain aircraft — which
 * of the four populations it belongs to:
 *
 *                       ADS-B (bright)     no transponder (dark)
 *   simulated spawn     TRUTH              TRUTH_DARK
 *   live-feed mirror    TRUTH_LIVE         TRUTH_LIVE_DARK
 *
 * Simulated truth is the neutral ramp (near-black / near-white, then grey);
 * live-feed truth is the teal ramp.  Bright vs dark within a ramp is the
 * transponder question, neutral vs teal is the provenance question, so each
 * axis is readable on its own.  One resolver, used by the live map's canvas
 * layer, the aircraft list, the legend and the Physics tab's preview map, so
 * the same object is the same colour everywhere.
 *
 * `has_adsb` and `source` are compared strictly: an entry from an older
 * payload that carries neither keeps the plain simulated-ADS-B colour rather
 * than being read as dark or live. ── */

export type TruthClass = "anomalous" | "drone" | "sim_adsb" | "sim_dark" | "live_adsb" | "live_dark";

export interface TruthLike {
  is_anomalous?: boolean;
  object_type?: string;
  has_adsb?: boolean;
  source?: string;
}

export function truthClass(ac: TruthLike): TruthClass {
  if (ac.is_anomalous) return "anomalous";
  if (ac.object_type === "drone") return "drone";
  const dark = ac.has_adsb === false;
  if (ac.source === "live") return dark ? "live_dark" : "live_adsb";
  return dark ? "sim_dark" : "sim_adsb";
}

export function truthFill(cls: TruthClass, p: MapPalette): string {
  switch (cls) {
    case "anomalous": return p.ANOMALY;
    case "drone": return p.DRONE;
    case "live_adsb": return p.TRUTH_LIVE;
    case "live_dark": return p.TRUTH_LIVE_DARK;
    case "sim_dark": return p.TRUTH_DARK;
    default: return p.TRUTH;
  }
}

/* Each dot takes an edge a shade darker than its own fill — the same per-class
   edges in both themes, since a near-black rim separates a fill from either
   basemap.  Selection is the amber the map already uses for a selected glyph,
   its arcs and its trail. */
const BORDERS: Record<TruthClass, string> = {
  anomalous: "#991b1b", // red-800
  drone: "#b45309", // amber-700
  sim_adsb: "#020617", // slate-950
  sim_dark: "#334155", // slate-700
  live_adsb: "#042f2e", // teal-950
  live_dark: "#115e59", // teal-800
};

export function truthBorder(cls: TruthClass, selected: boolean, p: MapPalette): string {
  return selected ? p.SELECTED : BORDERS[cls];
}

/** Legend rows for the plain-aircraft classes, in the order the legend shows them. */
export function truthLegend(p: MapPalette): { cls: TruthClass; color: string; label: string }[] {
  return [
    { cls: "sim_adsb", color: p.TRUTH, label: "Truth: sim ADS-B" },
    { cls: "sim_dark", color: p.TRUTH_DARK, label: "Truth: sim dark" },
    { cls: "live_adsb", color: p.TRUTH_LIVE, label: "Truth: live ADS-B" },
    { cls: "live_dark", color: p.TRUTH_LIVE_DARK, label: "Truth: live dark" },
  ];
}
