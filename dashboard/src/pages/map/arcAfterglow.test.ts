import { afterEach, describe, expect, it, vi } from "vitest";
import { arcGeometry, arcOpacity } from "./arcAfterglow";
import { buildBistaticArc, type NodeGeometry } from "./bistaticArc";

// The replayed detection's own time, and the wall clock when it is replayed.
const T = Date.UTC(2026, 9, 18, 19, 50);
const WALL_MS = Date.UTC(2026, 9, 23, 12, 0);

const ARC: [number, number][] = [
  [34.0, -82.0],
  [34.1, -82.0],
  [34.2, -82.0],
];

// Where the registry says node-1 is today.
const NODE: NodeGeometry = {
  rx_lat: 34.0,
  rx_lon: -82.0,
  tx_lat: 34.0,
  tx_lon: -81.8,
  beam_azimuth_deg: 0,
  beam_width_deg: 42,
  max_range_km: 60,
  max_bistatic_range_km: null,
};

const entry = { ambiguity_arc: ARC, ts: T, node_ref: "node-1", delay_us: 46.2 };

afterEach(() => {
  vi.useRealTimers();
});

describe("arcOpacity", () => {
  it("keeps a replayed arc in its afterglow on the map's clock", () => {
    // Two seconds into a five-second fade.
    expect(arcOpacity(entry, T + 2_000)).toBeCloseTo(0.6, 10);

    // Aged on the wall clock, the same arc is days past its life.
    vi.useFakeTimers();
    vi.setSystemTime(WALL_MS);
    expect(arcOpacity(entry, Date.now())).toBeNull();
  });

  it("hides an arc past its life or stamped after the clock", () => {
    // ARC_TOTAL_LIFE_MS + 1 after the detection.
    expect(arcOpacity(entry, T + 5_001)).toBeNull();
    expect(arcOpacity(entry, T - 1)).toBeNull();
  });
});

describe("arcGeometry", () => {
  const nodesByRef = { "node-1": NODE };

  it("draws an archived arc along the locus it carries, not the registry's node", () => {
    expect(arcGeometry(entry, true, nodesByRef)).toBe(ARC);
  });

  it("rebuilds a live arc from the registry, and falls back to its own locus without the node", () => {
    const rebuilt = arcGeometry(entry, false, nodesByRef);
    expect(rebuilt).not.toBe(ARC);
    expect(rebuilt).toEqual(buildBistaticArc(46.2, NODE));
    expect(arcGeometry(entry, false, {})).toBe(ARC);
  });
});
