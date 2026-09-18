import { describe, it, expect } from "vitest";
import { MLAT_HISTORY_REFRESH_MS, newSolveArrived } from "./mlatHistory";

const at = (hex: string | null, seen: number | null) => ({ hex, seen });

describe("MLAT_HISTORY_REFRESH_MS", () => {
  it("keeps up with the 1-3 s dark solve cadence", () => {
    expect(MLAT_HISTORY_REFRESH_MS).toBe(3_000);
  });
});

describe("newSolveArrived", () => {
  it("fires when seen falls on the same track", () => {
    // 8 s old, then 0.4 s old: a solve landed between the two flushes.
    expect(newSolveArrived(at("mnabc123", 8), at("mnabc123", 0.4))).toBe(true);
  });

  it("does not fire while the same solve simply ages", () => {
    expect(newSolveArrived(at("mnabc123", 2), at("mnabc123", 5))).toBe(false);
    expect(newSolveArrived(at("mnabc123", 2), at("mnabc123", 2))).toBe(false);
  });

  it("does not compare ages across a selection change", () => {
    // The new track's seen is a different clock, and the selection effect
    // refetches from scratch anyway.
    expect(newSolveArrived(at("mnabc123", 30), at("mndef456", 1))).toBe(false);
    expect(newSolveArrived(at(null, null), at("mnabc123", 1))).toBe(false);
  });

  it("does not fire on a deselection", () => {
    expect(newSolveArrived(at("mnabc123", 8), at(null, null))).toBe(false);
  });

  it("needs an age on both sides", () => {
    expect(newSolveArrived(at("mnabc123", null), at("mnabc123", 1))).toBe(false);
    expect(newSolveArrived(at("mnabc123", 8), at("mnabc123", null))).toBe(false);
  });

  it("treats a zeroed age as a new solve", () => {
    // The backend clamps a negative age to 0, so 0 is a real value here.
    expect(newSolveArrived(at("mnabc123", 4), at("mnabc123", 0))).toBe(true);
  });
});
