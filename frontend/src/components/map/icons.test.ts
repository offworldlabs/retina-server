import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  getAircraftColor,
  drDriftM,
  drGsKt,
  drIconBudgetM,
  drIconState,
  hideDrIcon,
  isDarkMultinodeSolve,
  makeAircraftIcon,
} from "./icons";
import {
  DR_ICON_HIDE_DISTANCE_DARK_M,
  DR_ICON_HIDE_DISTANCE_M,
  DR_ICON_MAX_AGE_DARK_S,
  DR_UNKNOWN_GS_KT,
} from "./constants";
import { PALETTES, setActivePalette, type MapTheme } from "./mapPalette";

// Asserted against the palette rather than literal hex: the lane a source maps
// to is the contract, and the shade is free to move with the surface. The whole
// block runs once per theme, because the mapping is the contract in BOTH and a
// theme that quietly dropped a lane onto the wrong colour would otherwise pass.
describe.each<MapTheme>(["light", "dark"])("getAircraftColor lanes (%s)", (theme) => {
  const P = PALETTES[theme];
  const { ALT_BANDS, LANE_MN_ADSB, LANE_MN_DARK, LANE_SOLVER_SEED } = P;
  const ADSB_SINGLE_COLOR = P.LANE_ADSB_SINGLE;
  const SKY = LANE_MN_ADSB;
  const VIOLET = LANE_MN_DARK;

  beforeEach(() => setActivePalette(P));
  afterEach(() => setActivePalette(PALETTES.dark));

  it("colours an ADS-B-assisted multinode solve sky", () => {
    expect(getAircraftColor({ position_source: "multinode_solve", adsb_assisted: true })).toBe(SKY);
    // multinode flag alone (no position_source) takes the same branch.
    expect(getAircraftColor({ multinode: true, adsb_assisted: true })).toBe(SKY);
  });

  it("colours a dark multinode solve violet", () => {
    expect(getAircraftColor({ position_source: "multinode_solve", adsb_assisted: false })).toBe(VIOLET);
    // Absent flag is dark too — an older backend, or a lane that never set it.
    expect(getAircraftColor({ position_source: "multinode_solve" })).toBe(VIOLET);
  });

  it("colours a claimed single-node ADS-B target blue", () => {
    expect(getAircraftColor({ position_source: "adsb_single_node" })).toBe(ADSB_SINGLE_COLOR);
  });

  it("keeps the seeded-solver and fallback branches", () => {
    expect(getAircraftColor({ position_source: "solver_adsb_seed" })).toBe(LANE_SOLVER_SEED);
    expect(getAircraftColor({ position_source: "solver_single_node" })).toBe(SKY);
  });

  // The four lanes have to stay distinguishable; the documented exception is
  // the solver_single_node relic, which deliberately shares the sky lane.
  it("gives every lane its own colour", () => {
    const lanes = [SKY, VIOLET, ADSB_SINGLE_COLOR, LANE_SOLVER_SEED];
    expect(new Set(lanes).size).toBe(lanes.length);
  });

  it("lets colorByAlt override every lane", () => {
    const top = ALT_BANDS[0][1];
    const bottom = ALT_BANDS[ALT_BANDS.length - 1][1];
    const ac = { position_source: "multinode_solve", adsb_assisted: true, alt_baro: 41000 };
    expect(getAircraftColor(ac, true)).toBe(top);
    expect(getAircraftColor({ position_source: "adsb_single_node", alt_baro: 0 }, true)).toBe(bottom);
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

  it("keeps a non-solver target with no speed or age information visible", () => {
    // Nothing to dead-reckon: these lanes publish a real fix, not a projection.
    expect(hideDrIcon({}, NOW)).toBe(false);
    expect(hideDrIcon({ seen: 55 }, NOW)).toBe(false);
    expect(hideDrIcon({ gs: 450 }, NOW)).toBe(false);    // no seen, no gap → age 0
  });

  it("does not read an absent gs on a solve as a stationary aircraft", () => {
    // The old gate did exactly that, and the backend strips gs from entries
    // whose velocity it distrusts (VEL_TRUST_MODE=active) — so the entries with
    // the least trustworthy position were the only ones it never hid.
    const mn = { position_source: "multinode_solve", adsb_assisted: true, _updatedAt: NOW };
    expect(drGsKt(mn)).toBe(DR_UNKNOWN_GS_KT);
    expect(drDriftM({ ...mn, seen: 40 }, NOW)).toBeGreaterThan(DR_ICON_HIDE_DISTANCE_M);
    expect(hideDrIcon({ ...mn, seen: 40 }, NOW)).toBe(true);
    // Fresh is still fresh — the assumption only bites once time passes.
    expect(hideDrIcon({ ...mn, seen: 1 }, NOW)).toBe(false);
  });

  it("prefers the last stated ground speed over the assumed one", () => {
    const mn = { position_source: "multinode_solve", _updatedAt: NOW, _lastGsKt: 60 };
    expect(drGsKt(mn)).toBe(60);
    // 60 kt for 40 s is ~1.2 km — inside even the assisted budget.
    expect(hideDrIcon({ ...mn, adsb_assisted: true, seen: 40 }, NOW)).toBe(false);
    // A stated gs still wins over the carried one.
    expect(drGsKt({ ...mn, gs: 400 })).toBe(400);
    // 0 kt is a value, not an absence.
    expect(drGsKt({ ...mn, gs: 0 })).toBe(0);
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

describe("lane-aware drift budget", () => {
  const NOW = 1_000_000;
  const dark = (over: object = {}) =>
    ({ position_source: "multinode_solve", adsb_assisted: false, gs: 450, seen: 0, _updatedAt: NOW, ...over });
  const assisted = (over: object = {}) => dark({ adsb_assisted: true, ...over });

  it("gives only the dark multi-node lane the larger budget", () => {
    expect(drIconBudgetM(dark())).toBe(DR_ICON_HIDE_DISTANCE_DARK_M);
    // Absent flag is dark, exactly as getAircraftColor reads it.
    expect(drIconBudgetM({ position_source: "multinode_solve" })).toBe(DR_ICON_HIDE_DISTANCE_DARK_M);
    expect(drIconBudgetM({ multinode: true })).toBe(DR_ICON_HIDE_DISTANCE_DARK_M);
    expect(drIconBudgetM(assisted())).toBe(DR_ICON_HIDE_DISTANCE_M);
    expect(drIconBudgetM({ position_source: "adsb_single_node" })).toBe(DR_ICON_HIDE_DISTANCE_M);
    expect(DR_ICON_HIDE_DISTANCE_DARK_M).toBeGreaterThan(DR_ICON_HIDE_DISTANCE_M);
  });

  it("identifies the dark lane the same way the colour does", () => {
    expect(isDarkMultinodeSolve(dark())).toBe(true);
    expect(isDarkMultinodeSolve(assisted())).toBe(false);
    expect(isDarkMultinodeSolve({ position_source: "adsb_single_node" })).toBe(false);
    expect(isDarkMultinodeSolve(null)).toBe(false);
  });

  it("keeps a dark solve inside budget across a missed solve", () => {
    // 450 kt for 12 s is ~2.8 km: over the 2 km budget the old gate applied to
    // every lane, still inside the dark one.  This is the 48%-hidden bug.
    expect(hideDrIcon({ ...dark(), seen: 12 }, NOW)).toBe(false);
    expect(hideDrIcon({ ...assisted(), seen: 12 }, NOW)).toBe(true);
  });

  it("still trips the dark budget on a long enough gap", () => {
    // 450 kt for 15 s is ~3.5 km — past the 3 km dark budget.  Under the old
    // 6 km one this same entry was drawn as an ordinary live target.
    expect(hideDrIcon({ ...dark(), seen: 15 }, NOW)).toBe(true);
    expect(hideDrIcon({ ...dark(), seen: 30 }, NOW)).toBe(true);
  });
});

describe("drIconState", () => {
  const NOW = 1_000_000;
  const over = { gs: 450, seen: 40, _updatedAt: NOW };   // ~9.3 km of drift
  const darkOver = { ...over, position_source: "multinode_solve", adsb_assisted: false };
  const assistedOver = { ...over, position_source: "multinode_solve", adsb_assisted: true };
  // Over the 3 km dark drift budget (700 kt ≈ 360 m/s, so ~4.0 km) but still
  // inside the 12 s time budget — the one combination drawn in the stale style.
  const darkDrifted = { ...darkOver, gs: 700, seen: 11 };

  it("draws an in-budget track normally", () => {
    expect(drIconState({ ...darkOver, seen: 1 }, NOW)).toBe("normal");
    expect(drIconState({ ...assistedOver, seen: 1 }, NOW)).toBe("normal");
  });

  it("degrades rather than hides a drifted dark solve inside its time budget", () => {
    expect(hideDrIcon(darkDrifted, NOW)).toBe(true);
    expect(drIconState(darkDrifted, NOW)).toBe("stale");
  });

  it("hides an over-budget assisted solve — for that lane it is a real anomaly", () => {
    expect(drIconState(assistedOver, NOW)).toBe("hidden");
    expect(drIconState({ ...over, position_source: "adsb_single_node" }, NOW)).toBe("hidden");
  });

  it("never hides the selected aircraft, whatever its lane", () => {
    expect(drIconState(assistedOver, NOW, true)).toBe("stale");
    expect(drIconState({ ...over, position_source: "adsb_single_node" }, NOW, true)).toBe("stale");
    // Selection does not degrade a healthy icon.
    expect(drIconState({ ...assistedOver, seen: 1 }, NOW, true)).toBe("normal");
  });
});

describe("dark solve time budget", () => {
  const NOW = 1_000_000;
  // 120 kt ≈ 62 m/s: even 20 s of dead reckoning is ~1.2 km, inside both drift
  // budgets, so only the age rule can decide any of these.
  const dark = (over: object = {}) =>
    ({ position_source: "multinode_solve", adsb_assisted: false, gs: 120, seen: 0, _updatedAt: NOW, ...over });
  const assisted = (over: object = {}) => dark({ adsb_assisted: true, ...over });

  it("draws a dark solve normally right up to the budget", () => {
    expect(DR_ICON_MAX_AGE_DARK_S).toBe(12);
    expect(drIconState(dark({ seen: 11.9 }), NOW)).toBe("normal");
  });

  it("withdraws the icon past the budget however little the entry drifted", () => {
    // Measured: past 12 s of solve age 15% of dark entries are more than 5 km
    // from any aircraft.  Drift alone would have kept this one drawn.
    expect(hideDrIcon(dark({ seen: 12.1 }), NOW)).toBe(false);
    expect(drIconState(dark({ seen: 12.1 }), NOW)).toBe("hidden");
  });

  it("keeps a degraded icon for the selected aircraft past the budget", () => {
    expect(drIconState(dark({ seen: 12.1 }), NOW, true)).toBe("stale");
    expect(drIconState(dark({ seen: 300 }), NOW, true)).toBe("stale");
  });

  it("counts wall-clock time since ingest toward the solve age", () => {
    // 8 s at flush plus a 5 s WS gap is 13 s — the same age the disc grows on.
    const ac = dark({ seen: 8 });
    expect(drIconState(ac, NOW)).toBe("normal");
    expect(drIconState(ac, NOW + 5_000)).toBe("hidden");
  });

  it("brings the icon back when a new solve resets seen", () => {
    // About half of the tracks that go quiet for 12 s re-solve later (median
    // 16 s), and only the drawing was withdrawn, so they come straight back.
    expect(drIconState(dark({ seen: 20 }), NOW)).toBe("hidden");
    expect(drIconState(dark({ seen: 0.5 }), NOW)).toBe("normal");
  });

  it("leaves the other lanes on the drift rule alone", () => {
    // An assisted solve is anchored to a transponder fix, not extrapolated:
    // it keeps its icon at 20 s because 1.2 km is inside its 2 km budget.
    expect(drIconState(assisted({ seen: 20 }), NOW)).toBe("normal");
    expect(drIconState(assisted({ seen: 300 }), NOW)).toBe("hidden");   // drift, not age
    expect(drIconState({ position_source: "adsb_single_node", gs: 120, seen: 20, _updatedAt: NOW }, NOW)).toBe("normal");
  });
});

describe("makeAircraftIcon stale rendering", () => {
  const ac = { hex: "mnabc123", position_source: "multinode_solve", adsb_assisted: false, track: 90, alt_baro: 30000 };
  // The default theme, which is what makeAircraftIcon draws with unless a
  // provider has said otherwise.
  const VIOLET_ = PALETTES.dark.LANE_MN_DARK;

  it("keeps the lane colour and marks the marker stale", () => {
    const stale = makeAircraftIcon(ac, false, false, false, true);
    const html = stale.options.html as string;
    expect(stale.options.className).toContain("aircraft-marker-stale");
    // Still the hook the 60 fps rotation loop queries.
    expect(stale.options.className).toContain("ac-hex-mnabc123");
    expect(html).toContain(VIOLET_);            // colour is unchanged
    expect(html).toContain("stroke-dasharray");  // dashed outline
    expect(html).toContain("opacity:0.55");      // faded
  });

  it("leaves the normal icon solid and unmarked", () => {
    const normal = makeAircraftIcon(ac, false, false, false, false);
    const html = normal.options.html as string;
    expect(normal.options.className).not.toContain("aircraft-marker-stale");
    expect(html).not.toContain("stroke-dasharray");
    expect(html).not.toContain("opacity:0.55");
    expect(html).toContain(`fill="${VIOLET_}"`);
  });

  it("defaults to the solid rendering when the flag is omitted", () => {
    expect(makeAircraftIcon(ac, false, false, false).options.html)
      .toBe(makeAircraftIcon(ac, false, false, false, false).options.html);
  });
});

describe("makeAircraftIcon follows the active palette", () => {
  const ac = { hex: "mn1", position_source: "multinode_solve", adsb_assisted: true, track: 0, alt_baro: 30000 };
  const fillOf = () => {
    const html = makeAircraftIcon(ac, false, false, false).options.html as string;
    return html.match(/fill="(#[0-9a-f]{6})"/i)?.[1];
  };

  afterEach(() => setActivePalette(PALETTES.dark));

  // The icon factory reads the palette when it is CALLED, which is what lets a
  // theme switch reach markers already on the map — provided the caller
  // re-renders. AircraftMarker subscribes via usePalette() for exactly that
  // reason; this pins the half of the contract that can be tested headlessly.
  it("draws the lane in whichever palette is active", () => {
    setActivePalette(PALETTES.dark);
    expect(fillOf()).toBe(PALETTES.dark.LANE_MN_ADSB);
    setActivePalette(PALETTES.light);
    expect(fillOf()).toBe(PALETTES.light.LANE_MN_ADSB);
    expect(PALETTES.dark.LANE_MN_ADSB).not.toBe(PALETTES.light.LANE_MN_ADSB);
  });

  it("takes the halo and shadow from the palette too", () => {
    setActivePalette(PALETTES.light);
    const light = makeAircraftIcon(ac, false, false, false).options.html as string;
    setActivePalette(PALETTES.dark);
    const dark = makeAircraftIcon(ac, false, false, false).options.html as string;
    expect(light).toContain(PALETTES.light.ICON_SHADOW);
    expect(dark).toContain(PALETTES.dark.ICON_SHADOW);
    expect(light).toContain(`stroke-opacity="${PALETTES.light.ICON_HALO_OPACITY}"`);
    expect(dark).toContain(`stroke-opacity="${PALETTES.dark.ICON_HALO_OPACITY}"`);
  });
});
