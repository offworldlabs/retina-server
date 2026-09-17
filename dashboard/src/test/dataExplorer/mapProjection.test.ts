import { describe, expect, it } from "vitest";

import {
  centreExtent,
  fitExtent,
  gridLines,
  gridStep,
  project,
  ringRadii,
  unproject,
  VIEWPORT,
} from "../../pages/user/dataExplorer/mapProjection";
import type { RegistryNode } from "../../pages/user/dataExplorer/nodes";

function node(id: string, lat: number | null, lon: number | null): RegistryNode {
  return { id, name: id, status: null, synthetic: false, lat, lon, uncertaintyKm: null };
}

const LONDON = node("ret-london", 51.5074, -0.1278);
const OXFORD = node("ret-oxford", 51.752, -1.2577);
const BRIGHTON = node("ret-brighton", 50.8225, -0.1372);

describe("fitExtent", () => {
  it("is null when nothing has a position, so there is nothing to draw", () => {
    expect(fitExtent([node("a", null, null)])).toBeNull();
  });

  it("spans the positioned nodes", () => {
    const extent = fitExtent([LONDON, OXFORD, BRIGHTON])!;
    expect(extent.minLon).toBeLessThanOrEqual(-1.2577);
    expect(extent.maxLon).toBeGreaterThanOrEqual(-0.1278);
    expect(extent.minLat).toBeLessThanOrEqual(50.8225);
    expect(extent.maxLat).toBeGreaterThanOrEqual(51.752);
  });

  it("ignores nodes with no position rather than spanning to zero", () => {
    const extent = fitExtent([LONDON, node("ghost", null, null)])!;
    expect(extent.minLat).toBeGreaterThan(50);
  });

  it("gives a single node a usable box rather than a zero-width one", () => {
    const extent = fitExtent([LONDON])!;
    expect(extent.maxLon).toBeGreaterThan(extent.minLon);
    expect(extent.maxLat).toBeGreaterThan(extent.minLat);
  });
});

describe("project", () => {
  const extent = fitExtent([LONDON, OXFORD, BRIGHTON])!;

  it("places every node inside the viewport", () => {
    for (const n of [LONDON, OXFORD, BRIGHTON]) {
      const { x, y } = project(n.lat!, n.lon!, extent);
      expect(x).toBeGreaterThanOrEqual(0);
      expect(x).toBeLessThanOrEqual(VIEWPORT.width);
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y).toBeLessThanOrEqual(VIEWPORT.height);
    }
  });

  it("puts north at the top, which screen coordinates invert", () => {
    const north = project(OXFORD.lat!, OXFORD.lon!, extent);
    const south = project(BRIGHTON.lat!, BRIGHTON.lon!, extent);
    expect(north.y).toBeLessThan(south.y);
  });

  it("puts west to the left", () => {
    const west = project(OXFORD.lat!, OXFORD.lon!, extent);
    const east = project(LONDON.lat!, LONDON.lon!, extent);
    expect(west.x).toBeLessThan(east.x);
  });

  it("round-trips a projected point back to its position", () => {
    const { x, y } = project(LONDON.lat!, LONDON.lon!, extent);
    const back = unproject(x, y, extent);
    expect(back.lat).toBeCloseTo(LONDON.lat!, 4);
    expect(back.lon).toBeCloseTo(LONDON.lon!, 4);
  });
});

describe("the frame's shape", () => {
  /** Kilometres across, at the frame's own middle latitude. */
  const span = (extent: ReturnType<typeof fitExtent>) => {
    const e = extent!;
    const midLat = (e.minLat + e.maxLat) / 2;
    const cos = Math.cos((midLat * Math.PI) / 180);
    return {
      wide: (e.maxLon - e.minLon) * 111.32 * cos,
      tall: (e.maxLat - e.minLat) * 110.57,
    };
  };

  it("matches the box, so a radius draws round rather than stretched", () => {
    const { wide, tall } = span(fitExtent([LONDON, OXFORD, BRIGHTON]));
    expect(wide / tall).toBeCloseTo(VIEWPORT.width / VIEWPORT.height, 1);
  });

  it("holds that shape for a fleet spread along one axis", () => {
    const { wide, tall } = span(fitExtent([LONDON, node("far-north", 55.9, -0.13)]));
    expect(wide / tall).toBeCloseTo(VIEWPORT.width / VIEWPORT.height, 1);
  });

  it("draws the radius as a circle in the box's own units", () => {
    const extent = fitExtent([LONDON, OXFORD, BRIGHTON])!;
    const { rx, ry } = ringRadii(LONDON.lat!, 50, extent);
    expect(rx / ry).toBeCloseTo(1, 1);
  });
});

describe("centreExtent", () => {
  it("puts the centre in the middle of the frame", () => {
    const extent = centreExtent(51.5, -0.1, 50);
    expect((extent.minLat + extent.maxLat) / 2).toBeCloseTo(51.5, 6);
    expect((extent.minLon + extent.maxLon) / 2).toBeCloseTo(-0.1, 6);
  });

  it("leaves the ring room to be seen inside the frame", () => {
    const extent = centreExtent(51.5, -0.1, 50);
    const { ry } = ringRadii(51.5, 50, extent);
    expect(ry).toBeLessThan(VIEWPORT.height / 2);
    expect(ry).toBeGreaterThan(VIEWPORT.height / 8);
  });

  it("stops a tight radius zooming past anything recognisable", () => {
    const tight = centreExtent(51.5, -0.1, 1);
    expect(tight.maxLat - tight.minLat).toBeGreaterThan(0.5);
  });
});

describe("gridStep", () => {
  it("draws at most eight lines across a span", () => {
    for (const span of [0.3, 1, 4, 17, 60]) {
      expect(span / gridStep(span)).toBeLessThanOrEqual(8);
    }
  });

  it("puts a tick on every whole multiple of the step", () => {
    expect(gridLines(0.4, 2.1, 0.5)).toEqual([0.5, 1, 1.5, 2]);
  });

  it("ticks negative degrees the same way", () => {
    expect(gridLines(-1.2, 0.4, 0.5)).toEqual([-1, -0.5, 0]);
  });
});
