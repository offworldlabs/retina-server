import { describe, it, expect } from "vitest";
import { isSyntheticNode } from "./nodeKind";

describe("isSyntheticNode", () => {
  it("trusts the server flag over the identifier", () => {
    expect(isSyntheticNode({ is_synthetic: true }, "nde4f2k9xq7m3b8")).toBe(true);
    expect(isSyntheticNode({ is_synthetic: false }, "synth-GVL-0002")).toBe(false);
  });

  it("falls back to the prefix when the flag is absent", () => {
    expect(isSyntheticNode({}, "synth-GVL-0002")).toBe(true);
    expect(isSyntheticNode({}, "e2e-bulk-a-mstizad8")).toBe(true);
    expect(isSyntheticNode({}, "nde4f2k9xq7m3b8")).toBe(false);
  });
});
