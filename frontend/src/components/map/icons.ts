import L from "leaflet";
import {
  DR_ICON_HIDE_DISTANCE_DARK_M,
  DR_ICON_HIDE_DISTANCE_M,
  DR_ICON_MAX_AGE_DARK_S,
  DR_UNKNOWN_GS_KT,
  POSITION_SOURCE_ADSB_SINGLE,
} from "./constants";
// Same age the uncertainty disc grows on — one definition, so the icon and the
// disc can never disagree about how old a solve is.
import { solveAgeS } from "./uncertainty";
import { activePalette } from "./mapPalette";

// Top-down airplane SVG path (nose pointing up/north at 0°)
export const PLANE_PATH =
  "M16,2 C15.3,5.5 14.7,9 14.7,13 L3,20 L3,23 L14.7,19 L14.7,26 L11.5,28 L11.5,30.5 L16,29 L20.5,30.5 L20.5,28 L17.3,26 L17.3,19 L29,23 L29,20 L17.3,13 C17.3,9 16.7,5.5 16,2Z";

// Quadrotor drone SVG — simple X-frame with four motor circles.  A function
// rather than a constant: it bakes in a palette value, and the palette is now
// per-theme, so a module-level string would freeze whichever theme happened to
// load first.
export const droneSvg = (DRONE: string) => `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24"
  style="display:block;filter:drop-shadow(0 1px 3px rgba(15,23,42,0.35));">
  <!-- arms -->
  <line x1="4" y1="4" x2="20" y2="20" stroke="${DRONE}" stroke-width="2.2" stroke-linecap="round"/>
  <line x1="20" y1="4" x2="4" y2="20" stroke="${DRONE}" stroke-width="2.2" stroke-linecap="round"/>
  <!-- motor circles -->
  <circle cx="4"  cy="4"  r="3" fill="none" stroke="${DRONE}" stroke-width="1.5"/>
  <circle cx="20" cy="4"  r="3" fill="none" stroke="${DRONE}" stroke-width="1.5"/>
  <circle cx="4"  cy="20" r="3" fill="none" stroke="${DRONE}" stroke-width="1.5"/>
  <circle cx="20" cy="20" r="3" fill="none" stroke="${DRONE}" stroke-width="1.5"/>
  <!-- center hub -->
  <circle cx="12" cy="12" r="2.5" fill="${DRONE}"/>
</svg>`;

// Altitude → colour bands (low warm → high cool), FlightRadar-style. Band edges
// are multiples of 5000 ft so they line up with the AircraftMarker altBand
// memo key — crossing a band re-renders the icon with the new colour.
export function altitudeColor(altFt) {
  const { ALT_BANDS } = activePalette();
  for (const [floor, color] of ALT_BANDS) if (altFt >= floor) return color;
  return ALT_BANDS[ALT_BANDS.length - 1][1];
}

// Legend order is low band first, which is the reverse of the lookup order.
export function altitudeLegend(): [string, string][] {
  return activePalette()
    .ALT_BANDS.map(([, color, label]): [string, string] => [color, label])
    .reverse();
}

export function getAircraftColor(ac, colorByAlt = false) {
  const { LANE_MN_ADSB, LANE_MN_DARK, LANE_SOLVER_SEED } = activePalette();
  if (colorByAlt && typeof ac.alt_baro === "number") return altitudeColor(ac.alt_baro);
  // Multi-node splits on adsb_assisted (backend: the mn-adsb-* / mn-dark-* key
  // prefix): a solve that knew the transponder is cyan, a dark one fuchsia.
  // See the palette note in constants.ts for why the lanes are coloured this way.
  if (ac.multinode || ac.position_source === "multinode_solve")
    return ac.adsb_assisted ? LANE_MN_ADSB : LANE_MN_DARK;
  if (ac.position_source === POSITION_SOURCE_ADSB_SINGLE)
    return activePalette().LANE_ADSB_SINGLE;
  if (ac.position_source === "solver_adsb_seed") return LANE_SOLVER_SEED;
  // Fallback, sharing cyan with the assisted multi-node lane: the only
  // source that lands here is the solver_single_node relic, which is rare
  // enough that the collision is cheaper than a fourth shade.
  return LANE_MN_ADSB;
}

/** True for any multi-node solve, either lane. */
export function isMultinodeSolve(ac): boolean {
  return !!ac && (!!ac.multinode || ac.position_source === "multinode_solve");
}

/** True for a DARK multi-node solve (backend key prefix mn-dark-*).  Same rule
 *  as the fuchsia branch of getAircraftColor, including "absent flag is dark". */
export function isDarkMultinodeSolve(ac): boolean {
  return isMultinodeSolve(ac) && !ac.adsb_assisted;
}

/** Drift budget in metres for this entry's lane — the dark lane gets a bigger
 *  one because a missed dark solve is ordinary and an assisted one is not.
 *  How long a dark entry may keep drifting is DR_ICON_MAX_AGE_DARK_S's job.
 *  See the DR_ICON_HIDE_DISTANCE_M block in constants.ts. */
export function drIconBudgetM(ac): number {
  return isDarkMultinodeSolve(ac) ? DR_ICON_HIDE_DISTANCE_DARK_M : DR_ICON_HIDE_DISTANCE_M;
}

/** Ground speed in knots to dead-reckon this entry with.
 *
 *  An ABSENT gs is not a slow aircraft.  The backend deletes gs from entries
 *  whose velocity it does not trust, so reading the absence as 0 kt made those
 *  entries un-hideable while trustworthy fast ones were hidden — the gate
 *  exactly inverted.  Fall back to the last speed the feed did state for this
 *  track (carried on the fix as _lastGsKt by LiveAircraftMap), then to a
 *  conservative cruise figure.  Non-solver lanes keep the old 0: their position
 *  is a real fix, not a projection, so there is nothing to be conservative
 *  about. */
export function drGsKt(ac): number {
  const gs = ac?.gs;
  if (typeof gs === "number" && Number.isFinite(gs)) return gs;
  const last = ac?._lastGsKt;
  if (typeof last === "number" && Number.isFinite(last)) return last;
  return isMultinodeSolve(ac) ? DR_UNKNOWN_GS_KT : 0;
}

// Dead-reckoned drift (metres) since the last real solve.  ac.seen is the
// backend's age-of-solve — it resets on every real solve and grows on the
// re-broadcasts in between — and the _updatedAt term covers a WS gap since
// the last ingest, which `seen` cannot know about.  gs is knots.
export function drDriftM(ac, nowMs: number): number {
  const ageS = (ac.seen ?? 0) + Math.max(0, (nowMs - (ac._updatedAt ?? nowMs)) / 1000);
  return drGsKt(ac) * 0.514444 * ageS;
}

/** True when the icon has dead-reckoned further than its lane's drift budget.
 *  What that means for rendering is drIconState's business — the track itself
 *  always stays alive.  See DR_ICON_HIDE_DISTANCE_M. */
export function hideDrIcon(ac, nowMs: number): boolean {
  return drDriftM(ac, nowMs) > drIconBudgetM(ac);
}

/** How this entry's icon should be drawn given its drift and its solve age:
 *
 *  - "normal" — inside its lane's drift budget and, on the dark lane, inside
 *               the time budget too: an ordinary icon.
 *  - "stale"  — over the drift budget but still within DR_ICON_MAX_AGE_DARK_S
 *               of its last solve, drawn in the degraded style anyway.  A DARK
 *               multi-node solve lands here because a missed solve is not a
 *               fault: hiding it would say "not solved" about a track that was
 *               solved.  A SELECTED aircraft lands here too, whatever its lane
 *               or age, matching the viewport cull's selected-hex bypass —
 *               asking to look at a track and being shown nothing is worse
 *               than being shown a marker that admits it is stale.
 *  - "hidden" — over budget on a lane that re-solves every few seconds, so the
 *               drawn position is no longer evidence of anything; or a dark
 *               solve that has gone DR_ICON_MAX_AGE_DARK_S without re-solving,
 *               which measures as a lost track rather than a cadence gap
 *               however little it has drifted.
 *
 *  The age rule is checked first so it wins over the dark lane's "stale"
 *  concession: past the time budget a dark entry is not a stale solve being
 *  honest about its drift, it is an entry the solver stopped confirming.
 *  Either way only the drawing is withdrawn — the track stays in the stores,
 *  the list, the trails and the selection, and the next solve resets `seen`.
 */
export type DrIconState = "normal" | "stale" | "hidden";

export function drIconState(ac, nowMs: number, isSelected = false): DrIconState {
  if (isDarkMultinodeSolve(ac) && solveAgeS(ac, nowMs) > DR_ICON_MAX_AGE_DARK_S)
    return isSelected ? "stale" : "hidden";
  if (!hideDrIcon(ac, nowMs)) return "normal";
  if (isSelected || isDarkMultinodeSolve(ac)) return "stale";
  return "hidden";
}

// Altitude → icon edge in px. Exported because the claimed-arc trim
// (ClaimedArcs) scales its screen length off the icon it is centred on, and
// the two must move together across a band crossing.
export function aircraftIconSize(ac) {
  const altFt = ac.alt_baro ?? 0;
  return altFt > 35000 ? 30 : altFt > 20000 ? 26 : altFt > 5000 ? 22 : 18;
}

/**
 * `isStale` draws the SAME lane-coloured aircraft, hollowed out: the body drops
 * to a 15% wash, the outline becomes the lane colour dashed instead of the
 * solid hairline, and the whole marker sits at 55% opacity with no shadow.  It
 * reads at a glance as "this outline is where the solve says it would be",
 * which is the honest claim for a track past its drift budget — and it is not
 * the same picture as an absent icon, which claims nothing was solved.
 */
export function makeAircraftIcon(ac, showLabel, isSelected, colorByAlt = false, isStale = false) {
  const track = ac.track ?? 0;
  const color = getAircraftColor(ac, colorByAlt);
  const label = ac.flight?.trim() || ac.hex?.slice(-6)?.toUpperCase() || "";
  // Nullish: FL0 (on the ground) is a value, not an absence.
  const alt = ac.alt_baro != null ? `FL${Math.round(ac.alt_baro / 100)}` : "";

  const size = aircraftIconSize(ac);
  const { SELECTED, ICON_HALO, ICON_HALO_OPACITY, ICON_SHADOW } = activePalette();

  // The selection glow is tight and opaque because a soft halo dissolves into
  // a light basemap.
  const glow = isSelected
    ? `filter:drop-shadow(0 0 5px ${SELECTED}) drop-shadow(0 0 2px ${SELECTED});`
    : isStale
      ? ""
      : `filter:${ICON_SHADOW};`;

  // The fill carries the lane, and nothing is allowed to dilute it: at 18px an
  // outline is a large fraction of the glyph, so an ink one drags every lane
  // towards the same dark blur and a thick white one washes them all pale.
  // Both were tried on the light surface. What is left is a hairline halo,
  // just enough to stop the glyph merging into the basemap, with the drop
  // shadow supplying the edge — and how heavy each has to be is a property of
  // the basemap behind it, so both come from the palette.
  const bodyAttrs = isStale
    ? `fill="${color}" fill-opacity="0.15" stroke="${color}" stroke-width="1.6" stroke-dasharray="3 2.5"`
    : `fill="${color}" stroke="${ICON_HALO}" stroke-opacity="${ICON_HALO_OPACITY}" stroke-width="0.7"`;

  // pointer-events: only the visible SVG + label are clickable.  The outer
  // 90×44 container would otherwise grab clicks in its empty 90% area —
  // particularly bad when icons are smaller (low-altitude).
  const svgHtml = `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 32 32"
    style="display:block;pointer-events:auto;transform:rotate(${track}deg);${glow}">
    <path ${bodyAttrs} stroke-linejoin="round"
      d="${PLANE_PATH}"/>
  </svg>`;

  const labelHtml =
    showLabel && label
      ? `<div class="aircraft-label" style="pointer-events:auto;">${label}${alt ? `<span class="aircraft-alt"> ${alt}</span>` : ""}</div>`
      : "";

  // The stale class is also the hook the RAF rotation loop and any future CSS
  // need; ac-hex-<hex> must stay on the element (see makeDroneIcon's note).
  return L.divIcon({
    className: `aircraft-marker ac-hex-${ac.hex}${isStale ? " aircraft-marker-stale" : ""}`,
    html: `<div title="${isStale ? "Stale solve — dead-reckoned past the drift budget" : ""}" style="display:flex;flex-direction:column;align-items:center;pointer-events:none;${isStale ? "opacity:0.55;" : ""}">${svgHtml}${labelHtml}</div>`,
    iconSize: [90, 44],
    iconAnchor: [45, Math.round(size / 2)],
  });
}

// NOTE: the ac-hex-<hex> class is what the 60 fps DOM-rotation loop queries;
// drone icons lacked it, so every drone burned a document-wide selector miss
// per frame and never rotated to its heading.
export function makeDroneIcon(ac, showLabel, isSelected) {
  const label = ac.flight?.trim() || ac.hex?.slice(-6)?.toUpperCase() || "";
  const { SELECTED, DRONE } = activePalette();
  const glowFilter = isSelected ? `filter:drop-shadow(0 0 5px ${SELECTED});` : "";

  const droneHtml = `<div style="${glowFilter}">${droneSvg(DRONE)}</div>`;
  const labelHtml =
    showLabel && label
      ? `<div class="aircraft-label" style="color:${DRONE};">${label}</div>`
      : "";

  return L.divIcon({
    className: `aircraft-marker ac-hex-${ac.hex}`,
    html: `<div style="display:flex;flex-direction:column;align-items:center;">${droneHtml}${labelHtml}</div>`,
    iconSize: [90, 40],
    iconAnchor: [45, 11],
  });
}

// Nodes use amber instead of red so they don't share a palette with the
// anomalous-aircraft marker (rose-red + dashed halo). The two reds were close
// enough that users mistook anomalous aircraft for static nodes.
//
// The rings carry the node on a pale basemap where the old yellow glow could
// not: a light halo on light tiles is invisible, so the outer rings are drawn
// at higher opacity and the glow is an ink drop instead.  A function rather
// than a constant, for the same reason droneSvg is one.
const _nodeIcons = new WeakMap<object, L.DivIcon>();

function nodeGlyphSvg({ NODE, ICON_SHADOW }: { NODE: string; ICON_SHADOW: string }) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24"
    style="display:block;filter:${ICON_SHADOW};">
    <circle cx="12" cy="12" r="3.2" fill="${NODE}"/>
    <circle cx="12" cy="12" r="6.5" fill="none" stroke="${NODE}" stroke-width="1.5" opacity="0.75"/>
    <circle cx="12" cy="12" r="10.5" fill="none" stroke="${NODE}" stroke-width="1" opacity="0.4"/>
  </svg>`;
}

export function nodeIcon() {
  const palette = activePalette();
  const cached = _nodeIcons.get(palette);
  if (cached) return cached;
  const icon = L.divIcon({
    className: "node-marker",
    html: nodeGlyphSvg(palette),
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
  _nodeIcons.set(palette, icon);
  return icon;
}

// One marker per receive SITE, so a site with two co-located receivers needs
// to say so: the glyph is unchanged (same size, same glow, same anchor) with a
// small count badge on its shoulder.  Two stacked glyphs used to be the only
// hint, and being identical and coincident they read as one node.  Cached per
// (palette, count) — a divIcon is immutable and a fleet has two or three
// distinct counts, so rebuilding one per render would churn DOM for nothing.
const _nodeSiteIcons = new WeakMap<object, Map<number, L.DivIcon>>();

export function nodeSiteIcon(count: number): L.DivIcon {
  if (!(count > 1)) return nodeIcon();
  const palette = activePalette();
  let byCount = _nodeSiteIcons.get(palette);
  if (!byCount) {
    byCount = new Map();
    _nodeSiteIcons.set(palette, byCount);
  }
  const cached = byCount.get(count);
  if (cached) return cached;
  const icon = L.divIcon({
    className: "node-marker",
    html: `<div style="position:relative;width:22px;height:22px;">${nodeGlyphSvg(palette)}<span class="node-badge">${count}</span></div>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
  byCount.set(count, icon);
  return icon;
}
