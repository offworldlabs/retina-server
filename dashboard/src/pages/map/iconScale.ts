import { useEffect, useState } from "react";
import { useMap, useMapEvents } from "react-leaflet";
import type { Map as LeafletMap } from "leaflet";

/* ── Zoom-aware marker scale.
 *
 * Every screen-sized glyph on the map — plane divIcons, node glyphs, the
 * truth and MLAT dots, the synthetic-node circles — used to be a fixed pixel
 * size at every zoom.  That is right at the default zoom (9, the metro
 * view) and wrong everywhere else: zoomed out to the state, fifty 22 px
 * node dots and thirty 26 px planes cover Greenville in an opaque pile, and
 * zoomed in to the runway the same planes are lost on the tiles.
 *
 * One function answers "how big should a glyph be at this zoom", relative
 * to its designed size at ICON_SCALE_REF_ZOOM.  Halving the scale every two
 * zoom steps keeps the glyph shrinking noticeably slower than the ground
 * (which halves every step), so it stays a recognisable icon rather than
 * a dot, and the clamps stop it vanishing at continental zoom or ballooning
 * on a runway.
 *
 * Two delivery routes, because the map has two kinds of glyph:
 *  - DOM markers (divIcons) read the CSS custom property IconScaleSync
 *    writes on the map container, so a zoom re-scales every marker in one
 *    style recalc without React touching a single marker.
 *  - Canvas circles (L.circleMarker radii) have no CSS to read, so their
 *    layers call iconZoomScale(map.getZoom()) when they (re)style a circle,
 *    and re-style on zoomend via useIconZoomScale.
 * ── */

/** Zoom at which every glyph is drawn at its designed pixel size. */
export const ICON_SCALE_REF_ZOOM = 9;

/** Zoom steps per halving of the scale (2 → a glyph halves every two steps). */
export const ICON_SCALE_STEPS_PER_HALVING = 2;

/** Floor: below this a plane silhouette is no longer readable as one. */
export const ICON_SCALE_MIN = 0.4;

/** Ceiling: zoomed in past the metro view the glyphs grow a little, not a lot. */
export const ICON_SCALE_MAX = 1.3;

/** Below this scale the callsign labels are hidden — at 9.5 px × 0.7 they
 *  are illegible smears that only add to the pile. */
export const ICON_SCALE_LABEL_MIN = 0.7;

/** CSS custom property IconScaleSync keeps on the Leaflet container. */
export const ICON_SCALE_CSS_VAR = "--map-icon-scale";

/** Data attribute on the container: "far" when labels should be hidden. */
export const ICON_SCALE_TIER_ATTR = "data-icon-tier";

/** Multiplier for a glyph's designed pixel size at map zoom `zoom`. */
export function iconZoomScale(zoom: number): number {
  if (!Number.isFinite(zoom)) return 1;
  const raw = Math.pow(2, (zoom - ICON_SCALE_REF_ZOOM) / ICON_SCALE_STEPS_PER_HALVING);
  return Math.min(ICON_SCALE_MAX, Math.max(ICON_SCALE_MIN, raw));
}

/** "far" when labels should be hidden at this scale, else "near". */
export function iconScaleTier(scale: number): "far" | "near" {
  return scale < ICON_SCALE_LABEL_MIN ? "far" : "near";
}

/** Write the current scale onto the map container (CSS var + tier attr).
 *  Idempotent and cheap; safe to call on every zoom event. */
export function applyIconScale(map: LeafletMap): number {
  const scale = iconZoomScale(map.getZoom());
  const el = map.getContainer();
  if (el) {
    el.style.setProperty(ICON_SCALE_CSS_VAR, scale.toFixed(3));
    el.setAttribute(ICON_SCALE_TIER_ATTR, iconScaleTier(scale));
  }
  return scale;
}

/** Mount once inside MapContainer.  Keeps the container's CSS variable in
 *  step with the zoom, including during the zoom animation ("zoom" fires
 *  per animation frame) so DOM markers shrink with the tiles instead of
 *  snapping at the end. */
export function IconScaleSync() {
  const map = useMapEvents({
    zoom: () => { applyIconScale(map); },
    zoomend: () => { applyIconScale(map); },
  });
  useEffect(() => { applyIconScale(map); }, [map]);
  return null;
}

/** The current zoom scale as React state — re-renders the caller on
 *  zoomend.  For layers that size L.circleMarker radii from React props. */
export function useIconZoomScale(): number {
  const map = useMap();
  const [scale, setScale] = useState(() => iconZoomScale(map.getZoom()));
  useMapEvents({
    zoomend: () => { setScale(iconZoomScale(map.getZoom())); },
  });
  return scale;
}
