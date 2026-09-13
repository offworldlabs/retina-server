import { describe, it, expect } from "vitest";
import {
  fromSyntheticNode,
  scrubToSyntheticNodes,
  syntheticDetectingNodes,
  type NodeAttributed,
} from "./syntheticOnly";

// A published real node reads as nde + 12, and matches no synthetic prefix.
const REAL = "nde7f3a91c0d42";
const REAL2 = "ndeb1c2d3e4f506";
const SYNTH = "synth-014";

describe("fromSyntheticNode", () => {
  it("keeps an entry claimed by a synthetic node", () => {
    expect(fromSyntheticNode({ node_ref: SYNTH })).toBe(true);
  });

  it("keeps a solve with at least one synthetic contributor", () => {
    expect(fromSyntheticNode({ contributing_node_refs: [REAL, SYNTH] })).toBe(true);
  });

  it("drops an entry naming only real nodes", () => {
    expect(fromSyntheticNode({ node_ref: REAL, contributing_node_refs: [REAL2] }))
      .toBe(false);
  });

  it("drops an entry naming no node at all", () => {
    expect(fromSyntheticNode({})).toBe(false);
    expect(fromSyntheticNode({ contributing_node_refs: [] })).toBe(false);
  });
});

describe("scrubToSyntheticNodes", () => {
  it("leaves only synthetic refs in a kept mixed entry", () => {
    const entry = {
      hex: "mn1234",
      node_ref: REAL,
      contributing_node_refs: [REAL, SYNTH, REAL2, "e2e-7"],
      n_nodes: 4,
      multinode: true,
    };
    // The filter keeps it: one contributor is synthetic.
    expect(fromSyntheticNode(entry)).toBe(true);

    const out = scrubToSyntheticNodes(entry);
    expect(out.contributing_node_refs).toEqual([SYNTH, "e2e-7"]);
    expect(out.node_ref).toBeUndefined();
    // The count follows the list it describes.
    expect(out.n_nodes).toBe(2);
    // Everything else survives.
    expect(out.hex).toBe("mn1234");
    expect(out.multinode).toBe(true);
  });

  it("does not mutate the entry it was given", () => {
    const entry = { node_ref: REAL, contributing_node_refs: [SYNTH, REAL2], n_nodes: 2 };
    scrubToSyntheticNodes(entry);
    expect(entry.node_ref).toBe(REAL);
    expect(entry.contributing_node_refs).toEqual([SYNTH, REAL2]);
    expect(entry.n_nodes).toBe(2);
  });

  it("returns an all-synthetic entry unchanged, count included", () => {
    const entry = { node_ref: SYNTH, contributing_node_refs: [SYNTH, "e2e-7"], n_nodes: 5 };
    const out = scrubToSyntheticNodes(entry);
    expect(out).toBe(entry);
    expect(out.n_nodes).toBe(5);
  });

  it("drops a real claimant from an entry with no contributing list", () => {
    // A single-node arc kept by nothing would not reach the scrub, but the
    // scrub must not depend on the filter's reasoning to be safe.
    const out = scrubToSyntheticNodes({ hex: "abc123", node_ref: REAL, delay_us: 12 });
    expect(out.node_ref).toBeUndefined();
    expect(out.delay_us).toBe(12);
  });

  it("leaves an entry with neither field alone", () => {
    const entry: NodeAttributed & { hex: string } = { hex: "abc123" };
    expect(scrubToSyntheticNodes(entry)).toBe(entry);
  });

  it("leaves n_nodes alone when the entry carries none", () => {
    const out = scrubToSyntheticNodes({ contributing_node_refs: [SYNTH, REAL] });
    expect(out.contributing_node_refs).toEqual([SYNTH]);
    expect("n_nodes" in out).toBe(false);
  });
});

describe("syntheticDetectingNodes", () => {
  it("drops real refs from each per-hex list", () => {
    expect(syntheticDetectingNodes({ abcdef: [SYNTH, REAL], other1: [REAL] }))
      .toEqual({ abcdef: [SYNTH], other1: [] });
  });

  it("tolerates a missing or malformed payload", () => {
    expect(syntheticDetectingNodes(undefined)).toEqual({});
    expect(syntheticDetectingNodes({ bad1: "not-a-list" } as never)).toEqual({ bad1: [] });
  });
});
