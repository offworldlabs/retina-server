import { describe, expect, it } from "vitest";
import {
  SCALED_WEIGHT_MIN_PX,
  scalePathOptions,
  scaledRadius,
  scaledWeight,
} from "./ScaledCircleMarker";
import { ICON_SCALE_MIN, ICON_SCALE_REF_ZOOM, iconZoomScale } from "./iconScale";

describe("scaledRadius", () => {
  it("leaves the designed radius alone at the reference zoom", () => {
    expect(scaledRadius(14, iconZoomScale(ICON_SCALE_REF_ZOOM))).toBe(14);
  });

  it("scales linearly and has no floor — a small dot may become a speck", () => {
    expect(scaledRadius(16, 0.5)).toBe(8);
    expect(scaledRadius(3, ICON_SCALE_MIN)).toBeCloseTo(1.2, 6);
  });
});

describe("scaledWeight", () => {
  it("thins a heavy stroke with the scale", () => {
    expect(scaledWeight(3, 0.5)).toBe(1.5);
    expect(scaledWeight(3, 1)).toBe(3);
  });

  it("never drops below the 1 px floor so the stroke does not vanish", () => {
    expect(scaledWeight(2, ICON_SCALE_MIN)).toBe(SCALED_WEIGHT_MIN_PX);
    expect(scaledWeight(1, 0.5)).toBe(SCALED_WEIGHT_MIN_PX);
    expect(SCALED_WEIGHT_MIN_PX).toBeGreaterThan(0);
  });

  it("passes through 'no stroke' and 'Leaflet default' unchanged", () => {
    expect(scaledWeight(0, 0.5)).toBe(0);
    expect(scaledWeight(undefined, 0.5)).toBeUndefined();
  });
});

describe("scalePathOptions", () => {
  it("returns the same object when there is no weight to scale, so react-leaflet does not re-style", () => {
    const opts = { color: "#fff", fillOpacity: 0.5 };
    expect(scalePathOptions(opts, 0.5)).toBe(opts);
    expect(scalePathOptions(undefined, 0.5)).toBeUndefined();
  });

  it("copies the options with only the weight changed", () => {
    const opts = { color: "#fff", weight: 3, dashArray: "5 5" };
    const out = scalePathOptions(opts, 0.5);
    expect(out).not.toBe(opts);
    expect(out).toEqual({ color: "#fff", weight: 1.5, dashArray: "5 5" });
    expect(opts.weight).toBe(3);
  });
});
