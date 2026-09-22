import { describe, expect, it } from "vitest";
import { distanceKm } from "../utils/geo";

describe("distanceKm", () => {
  it("is zero for a point against itself", () => {
    expect(distanceKm(51.5, -0.1, 51.5, -0.1)).toBe(0);
  });

  it("gives about 111.2 km for a degree of latitude", () => {
    expect(distanceKm(0, 0, 1, 0)).toBeCloseTo(111.19, 1);
    expect(distanceKm(51, 0, 52, 0)).toBeCloseTo(111.2, 0);
  });

  it("shortens a degree of longitude away from the equator", () => {
    expect(distanceKm(51, 0, 51, 1)).toBeLessThan(distanceKm(0, 0, 0, 1));
  });

  it("is symmetric", () => {
    const a = distanceKm(51.5, -0.13, 48.85, 2.35);
    const b = distanceKm(48.85, 2.35, 51.5, -0.13);
    expect(a).toBeCloseTo(b, 6);
  });

  it("is half the circumference between antipodes, not NaN", () => {
    expect(distanceKm(0, 0, 0, 180)).toBeCloseTo(Math.PI * 6371, 6);
  });
});
