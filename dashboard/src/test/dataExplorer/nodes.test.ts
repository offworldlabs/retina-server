import { describe, expect, it } from "vitest";

import {
  distanceKm,
  effectiveNodeIds,
  knownNodeIds,
  type RegistryNode,
} from "../../pages/user/dataExplorer/nodes";

function node(id: string, lat: number | null, lon: number | null, synthetic = false): RegistryNode {
  return { id, name: id, status: "online", synthetic, lat, lon, uncertaintyKm: null };
}

const REGISTRY = new Map<string, RegistryNode>([
  ["ret-london", node("ret-london", 51.5074, -0.1278)],
  ["ret-oxford", node("ret-oxford", 51.752, -1.2577)],
  ["ret-nowhere", node("ret-nowhere", null, null)],
  ["synth-1", node("synth-1", 51.5, -0.13, true)],
]);

const LONDON = { lat: 51.5074, lon: -0.1278, km: 10 };

describe("distanceKm", () => {
  it("is zero for a point against itself", () => {
    expect(distanceKm(51.5, -0.1, 51.5, -0.1)).toBe(0);
  });

  it("gives about 111 km for a degree of latitude", () => {
    expect(distanceKm(51, 0, 52, 0)).toBeCloseTo(111.2, 0);
  });

  it("shortens a degree of longitude away from the equator", () => {
    expect(distanceKm(51, 0, 51, 1)).toBeLessThan(distanceKm(0, 0, 0, 1));
  });
});

describe("knownNodeIds", () => {
  it("unions the registry with ids seen only in archive keys", () => {
    const ids = knownNodeIds(REGISTRY, new Set(["ret-london", "ret-fromkey"]));
    expect(ids).toContain("ret-fromkey");
    expect(ids).toContain("ret-oxford");
  });

  it("does not double-count a node in both", () => {
    const ids = knownNodeIds(REGISTRY, new Set(["ret-london"]));
    expect(ids.filter((id) => id === "ret-london")).toHaveLength(1);
  });

  it("sorts, so the picker order does not depend on arrival", () => {
    const ids = knownNodeIds(REGISTRY, new Set(["aaa-first"]));
    expect(ids[0]).toBe("aaa-first");
    expect([...ids]).toEqual([...ids].sort());
  });
});

describe("effectiveNodeIds", () => {
  it("is everything known when nothing is selected", () => {
    const eff = effectiveNodeIds(REGISTRY, new Set(["ret-fromkey"]), null, null);
    expect(eff.size).toBe(5);
    expect(eff.has("ret-fromkey")).toBe(true);
  });

  it("is empty for an empty selection, which is not the same as none", () => {
    const eff = effectiveNodeIds(REGISTRY, new Set(), new Set(), null);
    expect(eff.size).toBe(0);
  });

  it("keeps only the selected nodes", () => {
    const eff = effectiveNodeIds(REGISTRY, new Set(), new Set(["ret-oxford"]), null);
    expect([...eff]).toEqual(["ret-oxford"]);
  });

  it("keeps a node with no published position while no radius is set", () => {
    const eff = effectiveNodeIds(REGISTRY, new Set(), null, null);
    expect(eff.has("ret-nowhere")).toBe(true);
  });

  it("drops a node outside the radius", () => {
    const eff = effectiveNodeIds(REGISTRY, new Set(), null, LONDON);
    expect(eff.has("ret-london")).toBe(true);
    expect(eff.has("ret-oxford")).toBe(false);
  });

  it("never admits a node with no published position to a radius filter", () => {
    const eff = effectiveNodeIds(REGISTRY, new Set(), null, { ...LONDON, km: 20000 });
    expect(eff.has("ret-nowhere")).toBe(false);
  });

  it("never admits a node discovered from a key, which has no position either", () => {
    const eff = effectiveNodeIds(REGISTRY, new Set(["ret-fromkey"]), null, { ...LONDON, km: 20000 });
    expect(eff.has("ret-fromkey")).toBe(false);
  });

  it("intersects the selection with the radius rather than replacing it", () => {
    const selection = new Set(["ret-london", "ret-oxford"]);
    const eff = effectiveNodeIds(REGISTRY, new Set(), selection, LONDON);
    expect([...eff]).toEqual(["ret-london"]);
  });
});
