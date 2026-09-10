import { describe, it, expect } from "vitest";
import { PALETTES } from "./mapPalette";
import { truthBorder, truthClass, truthFill, truthLegend } from "./truthColor";

describe("truthClass", () => {
  it("splits plain aircraft into the four provenance × transponder classes", () => {
    expect(truthClass({ object_type: "aircraft", has_adsb: true, source: "sim" })).toBe("sim_adsb");
    expect(truthClass({ object_type: "aircraft", has_adsb: false, source: "sim" })).toBe("sim_dark");
    expect(truthClass({ object_type: "aircraft", has_adsb: true, source: "live" })).toBe("live_adsb");
    expect(truthClass({ object_type: "aircraft", has_adsb: false, source: "live" })).toBe("live_dark");
  });

  it("keeps anomalous and drone ahead of provenance", () => {
    expect(truthClass({ is_anomalous: true, source: "live", has_adsb: false })).toBe("anomalous");
    expect(truthClass({ object_type: "drone", source: "live", has_adsb: false })).toBe("drone");
  });

  it("reads an older payload with neither field as simulated ADS-B", () => {
    expect(truthClass({})).toBe("sim_adsb");
    expect(truthClass({ object_type: "aircraft" })).toBe("sim_adsb");
  });
});

describe("truthFill / truthBorder", () => {
  it.each(["light", "dark"] as const)("gives the four classes four distinct fills (%s)", (theme) => {
    const p = PALETTES[theme];
    const fills = (["sim_adsb", "sim_dark", "live_adsb", "live_dark"] as const).map((c) => truthFill(c, p));
    expect(new Set(fills).size).toBe(4);
    expect(fills).toEqual([p.TRUTH, p.TRUTH_DARK, p.TRUTH_LIVE, p.TRUTH_LIVE_DARK]);
  });

  it("uses the selection amber for a selected dot in either theme", () => {
    expect(truthBorder("live_dark", true, PALETTES.dark)).toBe(PALETTES.dark.SELECTED);
    expect(truthBorder("live_dark", false, PALETTES.dark)).not.toBe(PALETTES.dark.SELECTED);
  });

  it("legend rows carry the same colours the dots are drawn with", () => {
    const p = PALETTES.light;
    for (const row of truthLegend(p)) expect(row.color).toBe(truthFill(row.cls, p));
  });
});
