import { describe, it, expect } from "vitest";
import { nodeLabel, groupNodesBySite, polygonMaxReachKm } from "./nodeSites";
import type { RadarNode } from "../../types";

const node = (extra: Partial<RadarNode> = {}): RadarNode => ({
  node_ref: "nde0123456789",
  rx_lat: 34.85,
  rx_lon: -82.4,
  location_uncertainty_km: 1,
  tx_lat: 35.0,
  tx_lon: -82.4,
  rx_alt_m: null,
  tx_alt_m: null,
  beam_azimuth_deg: 0,
  beam_width_deg: 42,
  max_range_km: 50,
  max_bistatic_range_km: null,
  empirical_polygon: null,
  empirical_n_points: 0,
  is_synthetic: false,
  ...extra,
});

describe("nodeLabel", () => {
  it("is the node's public ref", () => {
    expect(nodeLabel(node({ node_ref: "ndeabcdef01234" }))).toBe("ndeabcdef01234");
  });

  it("never falls back to the node id", () => {
    // The ref is the only identifier on the wire, so a node that somehow
    // arrives without one has no name to print — never an id.
    expect(nodeLabel({ ...node(), node_ref: null } as unknown as RadarNode)).toBe(
      "unlisted node",
    );
    expect(nodeLabel(undefined)).toBe("unlisted node");
  });
});

describe("groupNodesBySite", () => {
  it("groups nodes published at equal coordinates into one site", () => {
    const sites = groupNodesBySite([
      node({ node_ref: "nde00000000002" }),
      node({ node_ref: "nde00000000001" }),
    ]);
    expect(sites).toHaveLength(1);
    expect(sites[0].nodes.map((n) => n.node_ref)).toEqual([
      "nde00000000001",
      "nde00000000002",
    ]); // sorted by ref
  });

  it("keeps coordinates that differ in the 5th decimal apart", () => {
    const sites = groupNodesBySite([
      node({ node_ref: "nde00000000001", rx_lat: 34.85 }),
      node({ node_ref: "nde00000000002", rx_lat: 34.85001 }),
    ]);
    expect(sites).toHaveLength(2);
  });

  it("takes the widest uncertainty at the site", () => {
    const sites = groupNodesBySite([
      node({ node_ref: "nde00000000001", location_uncertainty_km: 1 }),
      node({ node_ref: "nde00000000002", location_uncertainty_km: 3 }),
    ]);
    expect(sites[0].location_uncertainty_km).toBe(3);
  });

  it("is synthetic only when every node at the site is", () => {
    const synthOnly = groupNodesBySite([
      node({ node_ref: "nde00000000001", is_synthetic: true }),
      node({ node_ref: "nde00000000002", is_synthetic: true }),
    ]);
    expect(synthOnly[0].isSynth).toBe(true);

    const mixed = groupNodesBySite([
      node({ node_ref: "nde00000000001", is_synthetic: true }),
      node({ node_ref: "nde00000000002", is_synthetic: false }),
    ]);
    expect(mixed[0].isSynth).toBe(false);
  });

  it("returns nothing for no nodes", () => {
    expect(groupNodesBySite([])).toEqual([]);
  });
});

describe("polygonMaxReachKm", () => {
  it("is the distance to the furthest vertex, in whole km", () => {
    // 1° of latitude ≈ 111.19 km; the nearer vertex must not win.
    const poly: [number, number][] = [[0.05, 0], [1, 0], [0, 0.1]];
    expect(polygonMaxReachKm(0, 0, poly)).toBe(111);
  });

  it("is null when there is no polygon to measure", () => {
    expect(polygonMaxReachKm(0, 0, null)).toBeNull();
    expect(polygonMaxReachKm(0, 0, [])).toBeNull();
  });
});
