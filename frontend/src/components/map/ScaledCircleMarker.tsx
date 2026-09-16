import { forwardRef } from "react";
import { CircleMarker, type CircleMarkerProps } from "react-leaflet";
import type { CircleMarker as LeafletCircleMarker, PathOptions } from "leaflet";
import { useIconZoomScale } from "./iconScale";

/* ── ScaledCircleMarker: a react-leaflet CircleMarker whose pixel radius
 *    follows the zoom-aware icon scale (see iconScale.ts).
 *
 * A CircleMarker's radius is screen pixels, so the synthetic-node dots, the
 * user-location dot, the selection rings and the rest were the same size at
 * every zoom and piled up on top of each other once the map was zoomed out.
 * DOM markers fix that with a CSS variable; a canvas circle has no CSS to
 * read, so the multiplier has to reach its `radius` prop, and the prop has
 * to change on zoomend for react-leaflet to call setRadius.
 *
 * That needs useIconZoomScale, which needs the map — and most of these
 * circles are written straight into the MapContainer JSX of the top-level
 * component, where no hook can see the map.  This wrapper is what makes it
 * legal: every instance calls the hook for itself, so it is the only place
 * that knows the scale, and the call sites stay a one-word swap.
 *
 * Everything else (children, ref, className, eventHandlers, interactive,
 * bubblingMouseEvents, ...) is forwarded untouched.  `className` stays a
 * top-level prop, not a pathOptions field — react-leaflet drops it from
 * pathOptions, as the CoverageLayer note in LiveAircraftMap explains. ── */

/** Below this a scaled stroke is not drawn at all on a HiDPI canvas. */
export const SCALED_WEIGHT_MIN_PX = 1;

/** Designed radius times the zoom scale.  No floor: a 3 px history dot at
 *  0.4 is a 1.2 px speck by design — it is context, not a target. */
export function scaledRadius(radius: number, scale: number): number {
  return radius * scale;
}

/** Designed stroke width times the zoom scale, floored so a heavy stroke
 *  thins with its circle but never vanishes.  Undefined stays undefined
 *  (Leaflet's own default applies, unscaled); 0 stays 0 (no stroke). */
export function scaledWeight(weight: number | undefined, scale: number): number | undefined {
  if (weight == null || weight <= 0) return weight;
  return Math.max(SCALED_WEIGHT_MIN_PX, weight * scale);
}

/** pathOptions with the stroke weight scaled, or the same object when there
 *  is nothing to scale — a new object would make react-leaflet re-style the
 *  circle on every render. */
export function scalePathOptions(
  pathOptions: PathOptions | undefined,
  scale: number,
): PathOptions | undefined {
  if (!pathOptions || pathOptions.weight == null) return pathOptions;
  return { ...pathOptions, weight: scaledWeight(pathOptions.weight, scale) };
}

export interface ScaledCircleMarkerProps extends Omit<CircleMarkerProps, "radius"> {
  /** Designed pixel radius at the reference zoom. */
  radius: number;
  /** Also scale pathOptions.weight (floored at SCALED_WEIGHT_MIN_PX).  For
   *  the heavy-stroked rings; a 1-2 px stroke reads fine at every zoom. */
  scaleWeight?: boolean;
}

const ScaledCircleMarker = forwardRef<LeafletCircleMarker, ScaledCircleMarkerProps>(
  function ScaledCircleMarker({ radius, pathOptions, scaleWeight = false, ...rest }, ref) {
    const scale = useIconZoomScale();
    return (
      <CircleMarker
        ref={ref}
        radius={scaledRadius(radius, scale)}
        pathOptions={scaleWeight ? scalePathOptions(pathOptions, scale) : pathOptions}
        {...rest}
      />
    );
  },
);

export default ScaledCircleMarker;
