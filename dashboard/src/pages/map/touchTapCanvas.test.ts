import L from "leaflet";
import { describe, expect, it } from "vitest";
import { touchTapCanvas } from "./touchTapCanvas";

// Two plane dots as GroundTruthCanvasLayer draws them at the reference zoom:
// radius 9 and a 3px stroke, so 10.5px from centre to edge as drawn and
// 22.5px with a finger's 12px. Their centres are 20px apart, so the drawn
// dots overlap by a pixel and each one's reach covers the other's centre.
// B is drawn after A, on top of it.
const A = L.point(100, 100);
const B = L.point(120, 100);
const OVERLAP = L.point(110, 100); // 10px from both centres
const NEAR_A = L.point(108, 115); // 6.5px off A's edge, 8.7px off B's
const NEAR_B = L.point(112, 115); // the same, the other way round
const AWAY = L.point(100, 140); // beyond both reaches
const RADIUS = 9;
const WEIGHT = 3;

/** The renderer with dots on it, A and B unless `dots` places others, drawn
 *  in the order given, on a map that reports where an event landed and
 *  whether a drag has just moved the map. */
function canvasWithDots(
  tolerance: number,
  dots: readonly (readonly [string, L.Point])[] = [["A", A], ["B", B]],
) {
  const canvas = touchTapCanvas({ tolerance }) as any;
  const names = new Map<unknown, string>();
  for (const [name, at] of dots) {
    const dot = L.circleMarker([0, 0], { radius: RADIUS, weight: WEIGHT }) as any;
    dot._renderer = canvas;
    dot._point = at;
    canvas._initPath(dot); // drawn after every dot added before it
    names.set(dot, name);
  }
  const map = {
    at: A,
    dragged: false,
    fired: [] as unknown[][],
    mouseEventToLayerPoint: () => map.at,
    _draggableMoved: () => map.dragged,
    _fireDOMEvent: (...args: unknown[]) => { map.fired.push(args); },
  };
  canvas._map = map;

  /** The dot a `type` event at `at` goes to, or null for the map, as
   *  `onClick` finds it. */
  const tap = (at: L.Point, type = "click", onClick = canvas._onClick) => {
    map.at = at;
    map.fired = [];
    onClick.call(canvas, { type });
    expect(map.fired).toHaveLength(1);
    const [[, firedType, layers]] = map.fired as [unknown, string, unknown[] | false][];
    expect(firedType).toBe(type);
    return layers ? names.get(layers[0]) : null;
  };
  return { map, tap };
}

describe("a tap with a finger's reach (tolerance 12)", () => {
  it("goes to the dot drawn under it, though a dot drawn later reaches it", () => {
    const { tap } = canvasWithDots(12);
    expect(tap(A)).toBe("A");
    expect(tap(B)).toBe("B");
    // Where the two drawn dots overlap, the one on top, as a mouse finds it.
    expect(tap(OVERLAP)).toBe("B");
  });

  it("off every drawn dot, goes to the dot whose edge is nearest", () => {
    const { tap } = canvasWithDots(12);
    expect(tap(NEAR_A)).toBe("A");
    expect(tap(NEAR_B)).toBe("B");
    expect(tap(AWAY)).toBeNull();
  });

  it("counts a dot's stroke as part of the dot", () => {
    const edge = RADIUS + WEIGHT / 2;
    const drawnFirst = L.point(100, 100);
    const drawnLast = L.point(100 + edge + (RADIUS - 0.8), 100);
    const tapAt = L.point(100 + RADIUS - 0.4, 100);
    // Inside the first dot's fill, and inside the last dot's stroke ring only.
    expect(tapAt.distanceTo(drawnFirst)).toBeLessThan(RADIUS);
    expect(tapAt.distanceTo(drawnLast)).toBeGreaterThan(RADIUS);
    expect(tapAt.distanceTo(drawnLast)).toBeLessThanOrEqual(edge);
    const { tap } = canvasWithDots(12, [["B", drawnFirst], ["A", drawnLast]]);
    expect(tap(tapAt)).toBe("A");
  });

  it("gives a tap equally near two dots to the later drawn", () => {
    const edge = RADIUS + WEIGHT / 2;
    const drawnFirst = L.point(100, 100);
    const drawnLast = L.point(100 + 2 * edge + 9, 100);
    const tapAt = L.point(100 + edge + 4.5, 100);
    // Off both drawn dots by the same gap, and within a finger's reach of both.
    for (const dot of [drawnFirst, drawnLast]) {
      expect(tapAt.distanceTo(dot)).toBeGreaterThan(edge);
      expect(tapAt.distanceTo(dot) - edge).toBeLessThanOrEqual(12);
    }
    expect(tapAt.distanceTo(drawnFirst)).toBe(tapAt.distanceTo(drawnLast));
    const { tap } = canvasWithDots(12, [["A", drawnFirst], ["B", drawnLast]]);
    expect(tap(tapAt)).toBe("B");
  });

  it("lands on nothing straight after a drag, but the press that began it does", () => {
    const { map, tap } = canvasWithDots(12);
    map.dragged = true;
    expect(tap(A)).toBeNull();
    expect(tap(NEAR_A)).toBeNull();
    expect(tap(A, "mousedown")).toBe("A");
  });
});

describe("a click with a mouse (no tolerance)", () => {
  it("goes where Leaflet's own hit test sends it", () => {
    const { map, tap } = canvasWithDots(0);
    const leaflet = (L.Canvas.prototype as any)._onClick;
    for (const dragged of [false, true]) {
      map.dragged = dragged;
      for (const at of [A, B, OVERLAP, NEAR_A, AWAY]) {
        expect(tap(at)).toBe(tap(at, "click", leaflet));
      }
    }
    map.dragged = false;
    expect([A, B, OVERLAP, NEAR_A].map((at) => tap(at))).toEqual(["A", "B", "B", null]);
  });
});
