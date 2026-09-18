import { describe, expect, it } from "vitest";
import {
  ICON_SCALE_LABEL_MIN,
  ICON_SCALE_MAX,
  ICON_SCALE_MIN,
  ICON_SCALE_REF_ZOOM,
  iconScaleTier,
  iconZoomScale,
} from "./iconScale";

describe("iconZoomScale", () => {
  it("is exactly 1 at the reference zoom so the designed sizes are untouched there", () => {
    expect(iconZoomScale(ICON_SCALE_REF_ZOOM)).toBe(1);
  });

  it("halves every two zoom steps out and doubles every two in", () => {
    expect(iconZoomScale(ICON_SCALE_REF_ZOOM - 2)).toBeCloseTo(0.5, 6);
    expect(iconZoomScale(ICON_SCALE_REF_ZOOM - 1)).toBeCloseTo(Math.SQRT1_2, 6);
    expect(iconZoomScale(ICON_SCALE_REF_ZOOM + 0.5)).toBeCloseTo(Math.pow(2, 0.25), 6);
  });

  it("is monotonic in zoom", () => {
    let prev = -Infinity;
    for (let z = 0; z <= 18; z += 0.25) {
      const s = iconZoomScale(z);
      expect(s).toBeGreaterThanOrEqual(prev);
      prev = s;
    }
  });

  it("clamps at continental and runway zooms", () => {
    expect(iconZoomScale(0)).toBe(ICON_SCALE_MIN);
    expect(iconZoomScale(18)).toBe(ICON_SCALE_MAX);
    expect(ICON_SCALE_MIN).toBeLessThan(1);
    expect(ICON_SCALE_MAX).toBeGreaterThan(1);
  });

  it("falls back to 1 for a zoom that is not a number", () => {
    expect(iconZoomScale(NaN)).toBe(1);
    expect(iconZoomScale(undefined as unknown as number)).toBe(1);
  });
});

describe("iconScaleTier", () => {
  it("hides labels only once the scale drops below the label floor", () => {
    expect(iconScaleTier(1)).toBe("near");
    expect(iconScaleTier(ICON_SCALE_LABEL_MIN)).toBe("near");
    expect(iconScaleTier(ICON_SCALE_LABEL_MIN - 0.01)).toBe("far");
  });
});
