import { describe, expect, it } from "vitest";
import { mapLink } from "./mapLink";
import { parseHash } from "./useUrlHashState";

const NOW = Date.UTC(2026, 9, 19, 12, 0, 0);
const T = Date.UTC(2026, 9, 18, 22, 14, 0);
const BASE = { lat: 34.85, lon: -82.39, z: 10, t: T };

const hashOf = (link: string) => link.slice(link.indexOf("#"));

describe("mapLink", () => {
  it("writes the keys in the order the map writes them", () => {
    expect(mapLink(BASE)).toBe("/map#lat=34.8500&lon=-82.3900&z=10&t=2026-10-18T22:14:00Z");
    expect(mapLink({ ...BASE, hex: "mn0123456789", span: 20 })).toBe(
      "/map#lat=34.8500&lon=-82.3900&z=10&hex=mn0123456789&t=2026-10-18T22:14:00Z&span=20",
    );
  });

  it("floors t to the second", () => {
    expect(mapLink({ ...BASE, t: T + 7999 })).toContain("t=2026-10-18T22:14:07Z");
  });

  it.each([
    [0, "span=1"],
    [45, "span=30"],
    [14.6, "span=15"],
  ])("clamps a span of %s", (span, expected) => {
    expect(mapLink({ ...BASE, span })).toMatch(new RegExp(`&${expected}$`));
  });

  it("leaves out an empty hex", () => {
    expect(mapLink({ ...BASE, hex: "" })).toBe(mapLink(BASE));
    expect(mapLink({ ...BASE, hex: null })).toBe(mapLink(BASE));
  });

  it("returns an encoded hex intact through parseHash", () => {
    const link = mapLink({ ...BASE, hex: "a b&c" });
    expect(link).toContain("hex=a%20b%26c");
    expect(parseHash(hashOf(link), NOW).hex).toBe("a b&c");
  });

  it.each([
    [{ lat: 34.85, lon: -82.39, z: 10, t: T }],
    [{ lat: -33.8688, lon: 151.2093, z: 7, t: T + 7999, hex: "mn0123456789", span: 20 }],
    [{ lat: -0.5, lon: -122.4194, z: 14, t: T - 3600_000, span: 3 }],
  ])("parses back to the view it was built from: %j", (view) => {
    const { t, ...rest } = view;
    expect(parseHash(hashOf(mapLink(view)), NOW)).toEqual({
      ...rest,
      t: Math.floor(t / 1000) * 1000,
    });
  });

  it.each([
    ["lat", { lat: NaN }],
    ["lon", { lon: Infinity }],
    ["z", { z: NaN }],
    ["t", { t: NaN }],
  ])("throws a RangeError for a non-finite %s", (_name, bad) => {
    expect(() => mapLink({ ...BASE, ...bad })).toThrow(RangeError);
  });
});
