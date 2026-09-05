import { describe, it, expect } from "vitest";
import {
  UNCERTAINTY_K95,
  UNCERTAINTY_DR_CAP_S,
  UNCERTAINTY_MAX_RADIUS_M,
  solveAgeS,
  solveSigmaM,
  solveUncertaintyRadiusM,
} from "./uncertainty";

const NOW = 1_757_000_000_000;

/** A multi-node entry that arrived just now with the given sigmas. */
const mn = (extra = {}) => ({
  position_source: "multinode_solve",
  _updatedAt: NOW,
  seen: 0,
  ...extra,
});

describe("solveAgeS", () => {
  it("is the backend solve age when the message just arrived", () => {
    expect(solveAgeS(mn({ seen: 4 }), NOW)).toBe(4);
  });

  it("adds wall-clock time since ingest to the backend age", () => {
    // 4 s old at flush, message arrived 3 s ago → 7 s.
    expect(solveAgeS(mn({ seen: 4, _updatedAt: NOW - 3000 }), NOW)).toBeCloseTo(7, 6);
  });

  it("never goes backwards on a clock skew", () => {
    expect(solveAgeS(mn({ seen: 2, _updatedAt: NOW + 5000 }), NOW)).toBe(2);
  });

  it("treats missing seen / _updatedAt as a fresh solve", () => {
    expect(solveAgeS({ position_source: "multinode_solve" }, NOW)).toBe(0);
  });
});

describe("solveSigmaM", () => {
  it("returns the solve-epoch sigma at age 0", () => {
    expect(solveSigmaM(mn({ pos_sigma_m: 650 }), 0)).toBe(650);
  });

  it("grows in quadrature with the velocity sigma", () => {
    // sqrt(300² + (25·8)²) = sqrt(90000 + 40000) = 360.55…
    expect(solveSigmaM(mn({ pos_sigma_m: 300, pos_sigma_vel_ms: 25 }), 8)).toBeCloseTo(
      Math.sqrt(300 * 300 + 200 * 200),
      6,
    );
  });

  it("stops growing at the 60 s dead-reckoning cap", () => {
    const ac = mn({ pos_sigma_m: 200, pos_sigma_vel_ms: 25 });
    const atCap = solveSigmaM(ac, UNCERTAINTY_DR_CAP_S);
    expect(solveSigmaM(ac, 600)).toBe(atCap);
    expect(atCap).toBeCloseTo(Math.sqrt(200 * 200 + 1500 * 1500), 6);
  });

  it("clamps a negative age to the solve epoch", () => {
    expect(solveSigmaM(mn({ pos_sigma_m: 200, pos_sigma_vel_ms: 25 }), -30)).toBe(200);
  });

  it("returns null without a usable pos_sigma_m", () => {
    expect(solveSigmaM(mn(), 0)).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: null }), 0)).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: 0 }), 0)).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: -5 }), 0)).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: NaN }), 0)).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: Infinity }), 0)).toBeNull();
  });

  it("treats a missing or non-finite velocity sigma as no growth", () => {
    expect(solveSigmaM(mn({ pos_sigma_m: 400 }), 30)).toBe(400);
    expect(solveSigmaM(mn({ pos_sigma_m: 400, pos_sigma_vel_ms: NaN }), 30)).toBe(400);
    expect(solveSigmaM(mn({ pos_sigma_m: 400, pos_sigma_vel_ms: -10 }), 30)).toBe(400);
  });
});

describe("solveUncertaintyRadiusM", () => {
  it("is k95 × sigma at the solve epoch", () => {
    expect(solveUncertaintyRadiusM(mn({ pos_sigma_m: 650 }), NOW)).toBeCloseTo(
      UNCERTAINTY_K95 * 650,
      6,
    );
  });

  it("is 0 when the entry carries no sigma", () => {
    expect(solveUncertaintyRadiusM(mn(), NOW)).toBe(0);
    expect(solveUncertaintyRadiusM(null, NOW)).toBe(0);
  });

  it("is 0 for entries outside the multi-node lane", () => {
    expect(
      solveUncertaintyRadiusM(
        { position_source: "adsb_single_node", pos_sigma_m: 650, _updatedAt: NOW },
        NOW,
      ),
    ).toBe(0);
    expect(solveUncertaintyRadiusM({ pos_sigma_m: 650, _updatedAt: NOW }, NOW)).toBe(0);
  });

  it("accepts the multinode flag without a position_source", () => {
    expect(
      solveUncertaintyRadiusM({ multinode: true, pos_sigma_m: 200, _updatedAt: NOW }, NOW),
    ).toBeCloseTo(UNCERTAINTY_K95 * 200, 6);
  });

  it("grows while dead-reckoning, then holds at the 60 s cap", () => {
    const ac = mn({ pos_sigma_m: 200, pos_sigma_vel_ms: 25, _updatedAt: NOW - 10_000 });
    const fresh = solveUncertaintyRadiusM(mn({ pos_sigma_m: 200, pos_sigma_vel_ms: 25 }), NOW);
    const aged = solveUncertaintyRadiusM(ac, NOW);
    expect(aged).toBeGreaterThan(fresh);
    // Past the cap the radius stops moving.
    const at60 = solveUncertaintyRadiusM(
      mn({ pos_sigma_m: 200, pos_sigma_vel_ms: 25, _updatedAt: NOW - 60_000 }),
      NOW,
    );
    const at300 = solveUncertaintyRadiusM(
      mn({ pos_sigma_m: 200, pos_sigma_vel_ms: 25, _updatedAt: NOW - 300_000 }),
      NOW,
    );
    expect(at300).toBe(at60);
  });

  it("caps the radius at 10 km", () => {
    expect(solveUncertaintyRadiusM(mn({ pos_sigma_m: 5000 }), NOW)).toBe(
      UNCERTAINTY_MAX_RADIUS_M,
    );
    // A degenerate solve dead-reckoning at 150 m/s hits the ceiling too.
    expect(
      solveUncertaintyRadiusM(mn({ pos_sigma_m: 5000, pos_sigma_vel_ms: 150 }), NOW),
    ).toBe(UNCERTAINTY_MAX_RADIUS_M);
  });
});
