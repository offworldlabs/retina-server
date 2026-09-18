import { describe, expect, it } from "vitest";

import { placeLabels } from "../../pages/user/dataExplorer/mapLabels";
import { VIEWPORT } from "../../pages/user/dataExplorer/mapProjection";

const at = (id: string, x: number, y: number) => ({ id, text: id, x, y });

describe("placeLabels", () => {
  it("puts a label beside its dot", () => {
    const [label] = placeLabels([at("ret-a", 100, 100)]);
    expect(label.labelX).toBeGreaterThan(100);
    expect(label.labelY).toBeCloseTo(94);
  });

  it("flips a label to the left rather than running off the right edge", () => {
    const [label] = placeLabels([at("ret-a-very-long-id", VIEWPORT.width - 10, 100)]);
    expect(label.labelX).toBeLessThan(VIEWPORT.width - 10);
  });

  it("nudges a label clear of one already placed", () => {
    const [first, second] = placeLabels([at("ret-a", 100, 100), at("ret-b", 102, 101)]);
    expect(Math.abs(second.labelY - first.labelY)).toBeGreaterThanOrEqual(11);
  });

  it("drops a label it cannot place rather than overprinting", () => {
    // Ten ids on the same point: the nudge runs out before they all fit.
    const crowd = Array.from({ length: 10 }, (_, i) => at(`ret-${i}`, 100, 100));
    const placed = placeLabels(crowd);
    expect(placed.length).toBeGreaterThan(0);
    expect(placed.length).toBeLessThan(crowd.length);
  });

  it("leaves a sparse fleet entirely labelled", () => {
    const spread = Array.from({ length: 5 }, (_, i) => at(`ret-${i}`, 60 + i * 150, 40 + i * 40));
    expect(placeLabels(spread)).toHaveLength(5);
  });
});
