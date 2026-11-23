import L from "leaflet";

// Leaflet's canvas finds the layer under a click itself (Canvas._onClick): of
// the interactive layers within reach of the point, the one drawn last. The
// reach is the shape as drawn plus the renderer's `tolerance`, so with a
// finger's tolerance a dot's reach covers its crowded neighbours, and a tap
// dead centre on one dot went to a neighbour drawn after it.

/** What the hit test reads of a layer on the canvas (Leaflet 1.9). */
interface Hittable {
  options: L.PathOptions;
  _containsPoint(point: L.Point): boolean;
  _point: L.Point;
  _radius: number;
}

/** What the hit test reads of the canvas renderer itself (Leaflet 1.9). */
interface CanvasState {
  options: L.RendererOptions;
  _map: L.Map & { _draggableMoved(layer: Hittable): boolean };
  _drawFirst: { layer: Hittable; next: CanvasState["_drawFirst"] } | null;
  _fireEvent(layers: Hittable[] | false, e: MouseEvent): void;
}

const leafletClick = (L.Canvas.prototype as unknown as { _onClick(e: MouseEvent): void })._onClick;
const circleContains = (L.CircleMarker.prototype as unknown as Hittable)._containsPoint;

/** How far a circle's drawn edge is from its centre: its radius and half its
 *  stroke, which is Leaflet's own reach for it with no tolerance
 *  (CircleMarker._containsPoint, Path._clickTolerance). Null for a layer
 *  Leaflet does not hit-test as a circle. */
function drawnRadius(layer: Hittable): number | null {
  if (layer._containsPoint !== circleContains) return null;
  return layer._radius + (layer.options.stroke ? layer.options.weight / 2 : 0);
}

/**
 * A canvas renderer whose tolerance never takes a tap from the dot under it.
 *
 * A tap on a drawn dot goes to that dot, the one on top where two overlap,
 * as a mouse's click does. Only a tap that misses every drawn dot uses the
 * reach, and goes to the dot whose edge is nearest, the later drawn of two
 * as near. A tap lands on some dot exactly where it did before; only which
 * one can change. A layer that is not a circle keeps Leaflet's reach and
 * counts as under the tap. With no tolerance the hit test is Leaflet's own,
 * and hover is Leaflet's either way.
 */
export const TouchTapCanvas: new (options?: L.RendererOptions) => L.Canvas = L.Canvas.extend({
  _onClick(this: CanvasState, e: MouseEvent) {
    if (!this.options.tolerance) {
      leafletClick.call(this, e);
      return;
    }
    const point = this._map.mouseEventToLayerPoint(e);
    // As in Leaflet, a click straight after a drag lands on nothing it drags.
    const afterDrag = e.type === "click" || e.type === "preclick";
    let under: Hittable | null = null;
    let nearest: Hittable | null = null;
    let nearestGap = Infinity;
    for (let order = this._drawFirst; order; order = order.next) {
      const layer = order.layer;
      if (!layer.options.interactive || !layer._containsPoint(point)) continue;
      if (afterDrag && this._map._draggableMoved(layer)) continue;
      const radius = drawnRadius(layer);
      if (radius === null) {
        under = layer;
        continue;
      }
      const distance = point.distanceTo(layer._point);
      if (distance <= radius) {
        under = layer;
      } else if (distance - radius <= nearestGap) {
        nearest = layer;
        nearestGap = distance - radius;
      }
    }
    const clickedLayer = under ?? nearest;
    this._fireEvent(clickedLayer ? [clickedLayer] : false, e);
  },
});

export function touchTapCanvas(options?: L.RendererOptions): L.Canvas {
  return new TouchTapCanvas(options);
}
