/**
 * Placing nodes on the explorer's own map.
 *
 * There is no basemap and no tile source: a tile host would be a third party
 * watching who looks at what, and tiles would bring Leaflet into a page that
 * otherwise never loads it. An equirectangular frame with a graticule is
 * enough to place a radius against the published positions.
 *
 * The frame is fitted in kilometres, not degrees, and forced to the box's own
 * 3:1 shape, so a radius draws as a circle rather than as an ellipse whose
 * eccentricity is an artefact of the fleet's extent.
 */

import type { RegistryNode } from "./nodes";

export const VIEWPORT = { width: 900, height: 300 };

/** The box is three times as wide as it is tall, and the frame matches it. */
const ASPECT = VIEWPORT.width / VIEWPORT.height;

const KM_PER_DEG_LAT = 110.57;
const KM_PER_DEG_LON = 111.32;

/** Breathing room around the fleet: a share of its own span, plus a floor for
 *  the case of one node, or several sitting on top of each other. */
const SPAN_MARGIN_LAT = 1.3;
const SPAN_MARGIN_LON = 1.15;
const FLOOR_DEG = 0.5;
const FLOOR_KM = 50;

/** How much wider than the radius the centred view is. Above 2 the ring sits
 *  inside the frame with its surroundings visible either side. */
const CENTRE_ZOOM = 2.2;

/** The smallest centred view. Below this a tight radius zooms past anything
 *  recognisable. */
const MIN_CENTRE_KM = 40;

/** Degrees of longitude collapse towards the poles; the floor keeps a frame
 *  fitted near one from growing without bound. */
const MIN_COS_LAT = 0.1;

export interface Extent {
  minLat: number;
  maxLat: number;
  minLon: number;
  maxLon: number;
}

/** Fit all the nodes in, or zoom to the radius. */
export type MapMode = "fit" | "centre";

const toRadians = (degrees: number): number => (degrees * Math.PI) / 180;
const cosLat = (lat: number): number => Math.max(MIN_COS_LAT, Math.cos(toRadians(lat)));

function box(midLat: number, midLon: number, halfLatKm: number, halfLonKm: number): Extent {
  const dLat = halfLatKm / KM_PER_DEG_LAT;
  const dLon = halfLonKm / (KM_PER_DEG_LON * cosLat(midLat));
  return {
    minLat: midLat - dLat,
    maxLat: midLat + dLat,
    minLon: midLon - dLon,
    maxLon: midLon + dLon,
  };
}

/** The frame the radius filter sits in the middle of. */
export function centreExtent(lat: number, lon: number, km: number): Extent {
  const half = Math.max(km * CENTRE_ZOOM, MIN_CENTRE_KM);
  return box(lat, lon, half, half * ASPECT);
}

/**
 * The frame the positioned nodes fit in, or null when none of them can be
 * placed and there is nothing to draw. Whichever axis is the tighter is
 * widened to the box's shape, so the drawing never stretches.
 */
export function fitExtent(nodes: RegistryNode[]): Extent | null {
  const placed = nodes.filter((n) => n.lat !== null && n.lon !== null);
  if (!placed.length) return null;

  const lats = placed.map((n) => n.lat!);
  const lons = placed.map((n) => n.lon!);
  const midLat = (Math.max(...lats) + Math.min(...lats)) / 2;
  const midLon = (Math.max(...lons) + Math.min(...lons)) / 2;

  let halfLatKm =
    ((Math.max(...lats) - Math.min(...lats)) / 2) * SPAN_MARGIN_LAT * KM_PER_DEG_LAT +
    FLOOR_DEG * KM_PER_DEG_LAT;
  let halfLonKm =
    ((Math.max(...lons) - Math.min(...lons)) / 2) *
      SPAN_MARGIN_LON *
      KM_PER_DEG_LON *
      cosLat(midLat) +
    FLOOR_KM;

  if (halfLonKm < halfLatKm * ASPECT) halfLonKm = halfLatKm * ASPECT;
  else halfLatKm = halfLonKm / ASPECT;

  return box(midLat, midLon, halfLatKm, halfLonKm);
}

export function project(lat: number, lon: number, extent: Extent): { x: number; y: number } {
  const fx = (lon - extent.minLon) / (extent.maxLon - extent.minLon);
  // Screen y grows downwards, so north has to be flipped to reach the top.
  const fy = (extent.maxLat - lat) / (extent.maxLat - extent.minLat);
  return { x: fx * VIEWPORT.width, y: fy * VIEWPORT.height };
}

export function unproject(x: number, y: number, extent: Extent): { lat: number; lon: number } {
  return {
    lat: extent.maxLat - (y / VIEWPORT.height) * (extent.maxLat - extent.minLat),
    lon: extent.minLon + (x / VIEWPORT.width) * (extent.maxLon - extent.minLon),
  };
}

/** How far apart the radius filter's ring reaches, in viewport units. */
export function ringRadii(
  lat: number,
  km: number,
  extent: Extent,
): { rx: number; ry: number } {
  const rx =
    ((km / (KM_PER_DEG_LON * cosLat(lat))) / (extent.maxLon - extent.minLon)) * VIEWPORT.width;
  const ry = ((km / KM_PER_DEG_LAT) / (extent.maxLat - extent.minLat)) * VIEWPORT.height;
  return { rx, ry };
}

const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20];

/** The graticule interval that draws at most eight lines across a span. */
export function gridStep(span: number): number {
  return STEPS.find((step) => span / step <= 8) ?? 30;
}

/** Ticks at every multiple of the step inside the span. */
export function gridLines(from: number, to: number, step: number): number[] {
  const out: number[] = [];
  for (let v = Math.ceil(from / step) * step; v <= to; v += step) out.push(v);
  return out;
}
