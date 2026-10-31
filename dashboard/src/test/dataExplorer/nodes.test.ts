import { describe, expect, it } from "vitest";

import {
  centreOf,
  effectiveNodeIds,
  knownNodeIds,
  nodeMarks,
  type RegistryNode,
  scopeAdmits,
  selectionIn,
} from "../../pages/user/dataExplorer/nodes";
import { ownedNode } from "../fixtures/ownedNode";

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

describe("centreOf", () => {
  it("is a point where the point is", () => {
    expect(centreOf(LONDON, REGISTRY)).toBe(LONDON);
    expect(centreOf(null, REGISTRY)).toBeNull();
  });

  it("places a centre on a node where the registry puts the node", () => {
    expect(centreOf({ node: "ret-oxford", km: 25 }, REGISTRY)).toEqual({ lat: 51.752, lon: -1.2577, km: 25 });
  });

  it("places nothing for a node the registry does not hold, or holds with no position", () => {
    expect(centreOf({ node: "ret-elsewhere", km: 25 }, REGISTRY)).toBeNull();
    expect(centreOf({ node: "ret-nowhere", km: 25 }, REGISTRY)).toBeNull();
  });
});

const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", location_private: true });
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001" });
// Owned, but with no ref yet: nothing could name it.
const BARE = ownedNode({ node_id: "retbare0001", node_ref: null });
const OWNED = [ROOF, MAST, BARE];
const HILL = "ndehill00000001";

describe("scopeAdmits", () => {
  it("holds the owner's own nodes and no other in their archive", () => {
    const admits = scopeAdmits("mine", OWNED);
    expect([ROOF.node_ref, MAST.node_ref, HILL].map(admits)).toEqual([true, true, false]);
  });

  it("holds every node but the owner's private ones in the network's", () => {
    const admits = scopeAdmits("network", OWNED);
    expect([ROOF.node_ref, MAST.node_ref, HILL].map(admits)).toEqual([false, true, true]);
  });

  it("keeps nothing out of the network's with no owner's list to go by", () => {
    expect(scopeAdmits("network", [])(ROOF.node_ref)).toBe(true);
  });

  it("keeps out of the network's the private nodes an earlier list named", () => {
    const admits = scopeAdmits("network", [], new Set([ROOF.node_ref!]));
    expect([ROOF.node_ref, MAST.node_ref, HILL].map(admits)).toEqual([false, true, true]);
  });

  it("asks only the owner's own list what their archive holds, whatever an earlier list withheld", () => {
    const admits = scopeAdmits("mine", OWNED, new Set([ROOF.node_ref!, HILL]));
    expect([ROOF.node_ref, MAST.node_ref, HILL].map(admits)).toEqual([true, true, false]);
  });
});

describe("selectionIn", () => {
  const network = scopeAdmits("network", OWNED);

  it("drops the nodes the scope cannot list and keeps the rest", () => {
    expect(selectionIn(new Set([ROOF.node_ref, MAST.node_ref]), network)).toEqual(new Set([MAST.node_ref]));
  });

  it("falls back to every node when none of the selection is left", () => {
    expect(selectionIn(new Set([ROOF.node_ref]), network)).toBeNull();
  });

  it("leaves every node, and an explicit none, as they are", () => {
    expect(selectionIn(null, network)).toBeNull();
    const none = new Set<string>();
    expect(selectionIn(none, network)).toBe(none);
  });

  it("hands back the same selection when nothing is dropped", () => {
    const sel = new Set([MAST.node_ref, HILL]);
    expect(selectionIn(sel, network)).toBe(sel);
  });
});

describe("nodeMarks", () => {
  it("marks the owner's nodes on the network's lists", () => {
    expect(nodeMarks("network", OWNED)).toEqual(
      new Map([
        [ROOF.node_ref, "yours"],
        [MAST.node_ref, "yours"],
      ]),
    );
  });

  it("marks only the private ones on the owner's own, where every node is theirs", () => {
    expect(nodeMarks("mine", OWNED)).toEqual(new Map([[ROOF.node_ref, "private"]]));
  });

  it("marks nothing for a visitor", () => {
    expect(nodeMarks("network", []).size).toBe(0);
  });
});
