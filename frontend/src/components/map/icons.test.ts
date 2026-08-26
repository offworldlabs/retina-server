import { describe, it, expect } from "vitest";
import { getAircraftColor, drDriftM, hideDrIcon } from "./icons";
import { ADSB_SINGLE_COLOR, DR_ICON_HIDE_DISTANCE_M } from "./constants";

const CYAN = "#38bdf8";
const VIOLET = "#a78bfa";

describe("getAircraftColor lanes", () => {
  it("colours an ADS-B-assisted multinode solve cyan", () => {
    expect(getAircraftColor({ position_source: "multinode_solve", adsb_assisted: true })).toBe(CYAN);
    // multinode flag alone (no position_source) takes the same branch.
    expect(getAircraftColor({ multinode: true, adsb_assisted: true })).toBe(CYAN);
  });

  it("colours a dark multinode solve violet", () => {
    expect(getAircraftColor({ position_source: "multinode_solve", adsb_assisted: false })).toBe(VIOLET);
    // Absent flag is dark too — an older backend, or a lane that never set it.
    expect(getAircraftColor({ position_source: "multinode_solve" })).toBe(VIOLET);
  });

  it("colours a claimed single-node ADS-B target blue", () => {
    expect(getAircraftColor({ position_source: "adsb_single_node" })).toBe(ADSB_SINGLE_COLOR);
    expect(ADSB_SINGLE_COLOR).not.toBe(CYAN);
  });

  it("keeps the seeded-solver and fallback branches", () => {
    expect(getAircraftColor({ position_source: "solver_adsb_seed" })).toBe("#2dd4bf");
    expect(getAircraftColor({ position_source: "solver_single_node" })).toBe(CYAN);
  });

  it("lets colorByAlt override every lane", () => {
    const ac = { position_source: "multinode_solve", adsb_assisted: true, alt_baro: 41000 };
    expect(getAircraftColor(ac, true)).toBe("#a855f7");
    expect(getAircraftColor({ position_source: "adsb_single_node", alt_baro: 0 }, true)).toBe("#ef4444");
  });
});

describe("drDriftM / hideDrIcon", () => {
  const NOW = 1_000_000;
  // 450 kt ≈ 231 m/s — an airliner.  It crosses the 2 km budget at ~8.6 s.
  const fast = (over: object = {}) => ({ gs: 450, seen: 0, _updatedAt: NOW, ...over });

  it("keeps a freshly solved fast target visible", () => {
    expect(drDriftM(fast(), NOW)).toBe(0);
    expect(hideDrIcon(fast({ seen: 1.5 }), NOW)).toBe(false);
  });

  it("hides a fast target whose solves stopped", () => {
    expect(drDriftM(fast({ seen: 40 }), NOW)).toBeGreaterThan(DR_ICON_HIDE_DISTANCE_M);
    expect(hideDrIcon(fast({ seen: 40 }), NOW)).toBe(true);
  });

  it("keeps a stationary target visible however old the solve is", () => {
    expect(hideDrIcon({ gs: 0, seen: 55, _updatedAt: NOW }, NOW)).toBe(false);
  });

  it("keeps a target with no speed or age information visible", () => {
    expect(hideDrIcon({}, NOW)).toBe(false);
    expect(hideDrIcon({ seen: 55 }, NOW)).toBe(false);   // no gs → no known drift
    expect(hideDrIcon({ gs: 450 }, NOW)).toBe(false);    // no seen, no gap → age 0
  });

  it("adds the WS-gap term on top of seen", () => {
    // seen alone is under budget; a 6 s ingest gap pushes the same entry over.
    const ac = fast({ seen: 4 });
    expect(hideDrIcon(ac, NOW)).toBe(false);
    expect(hideDrIcon(ac, NOW + 6_000)).toBe(true);
    expect(drDriftM(ac, NOW + 6_000)).toBeGreaterThan(drDriftM(ac, NOW));
  });

  it("never counts a clock skew backwards as negative age", () => {
    expect(drDriftM(fast({ seen: 2 }), NOW - 5_000)).toBe(drDriftM(fast({ seen: 2 }), NOW));
  });

  it("revives the icon when a new solve resets seen", () => {
    expect(hideDrIcon(fast({ seen: 40 }), NOW)).toBe(true);
    expect(hideDrIcon(fast({ seen: 0.4 }), NOW)).toBe(false);
  });
});
