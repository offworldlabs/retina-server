import { describe, it, expect } from "vitest";
import {
  UNCERTAINTY_K95,
  UNCERTAINTY_MAX_RADIUS_M,
  solveAgeS,
  solveSigmaM,
  solveDiscCenter,
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
  it("is the published solve-epoch sigma", () => {
    expect(solveSigmaM(mn({ pos_sigma_m: 650 }))).toBe(650);
  });

  it("returns null without a usable pos_sigma_m", () => {
    expect(solveSigmaM(mn())).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: null }))).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: 0 }))).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: -5 }))).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: NaN }))).toBeNull();
    expect(solveSigmaM(mn({ pos_sigma_m: Infinity }))).toBeNull();
  });

  it("ignores the velocity sigma entirely", () => {
    // pos_sigma_vel_ms stays on the wire for the panel and the history, but
    // it no longer inflates the drawn radius.
    expect(solveSigmaM(mn({ pos_sigma_m: 400, pos_sigma_vel_ms: 150 }))).toBe(400);
  });
});

describe("solveDiscCenter", () => {
  it("is the solve-epoch position when the feed carries one", () => {
    expect(
      solveDiscCenter(mn({ lat: 33.95, lon: -84.5, solve_lat: 33.9, solve_lon: -84.6 })),
    ).toEqual([33.9, -84.6]);
  });

  it("falls back to the dead-reckoned position on an older backend", () => {
    expect(solveDiscCenter(mn({ lat: 33.95, lon: -84.5 }))).toEqual([33.95, -84.5]);
  });

  it("falls back when only one half of the solve pair is usable", () => {
    expect(solveDiscCenter(mn({ lat: 33.95, lon: -84.5, solve_lat: 33.9 }))).toEqual([
      33.95, -84.5,
    ]);
    expect(
      solveDiscCenter(mn({ lat: 33.95, lon: -84.5, solve_lat: NaN, solve_lon: -84.6 })),
    ).toEqual([33.95, -84.5]);
  });

  it("is null when no usable position is on the entry", () => {
    expect(solveDiscCenter(mn())).toBeNull();
    expect(solveDiscCenter(null)).toBeNull();
    expect(solveDiscCenter(mn({ lat: 33.95 }))).toBeNull();
  });

  it("accepts a solve at the origin (0 is a coordinate, not a miss)", () => {
    expect(solveDiscCenter(mn({ lat: 1, lon: 2, solve_lat: 0, solve_lon: 0 }))).toEqual([0, 0]);
  });
});

describe("solveUncertaintyRadiusM", () => {
  it("is k95 × the solve-epoch sigma", () => {
    expect(solveUncertaintyRadiusM(mn({ pos_sigma_m: 650 }))).toBeCloseTo(
      UNCERTAINTY_K95 * 650,
      6,
    );
  });

  it("is 0 when the entry carries no sigma", () => {
    expect(solveUncertaintyRadiusM(mn())).toBe(0);
    expect(solveUncertaintyRadiusM(null)).toBe(0);
  });

  it("is 0 for entries outside the multi-node lane", () => {
    expect(
      solveUncertaintyRadiusM({
        position_source: "adsb_single_node",
        pos_sigma_m: 650,
        _updatedAt: NOW,
      }),
    ).toBe(0);
    expect(solveUncertaintyRadiusM({ pos_sigma_m: 650, _updatedAt: NOW })).toBe(0);
  });

  it("accepts the multinode flag without a position_source", () => {
    expect(solveUncertaintyRadiusM({ multinode: true, pos_sigma_m: 200 })).toBeCloseTo(
      UNCERTAINTY_K95 * 200,
      6,
    );
  });

  it("does not grow with solve age", () => {
    // Same solve, seen fresh and 300 s later: the radius describes the
    // measurement, and the measurement did not change.
    const fresh = solveUncertaintyRadiusM(mn({ pos_sigma_m: 200, pos_sigma_vel_ms: 150 }));
    const aged = solveUncertaintyRadiusM(
      mn({ pos_sigma_m: 200, pos_sigma_vel_ms: 150, seen: 30, _updatedAt: NOW - 300_000 }),
    );
    expect(aged).toBe(fresh);
    expect(fresh).toBeCloseTo(UNCERTAINTY_K95 * 200, 6);
  });

  it("caps the radius at 10 km", () => {
    expect(solveUncertaintyRadiusM(mn({ pos_sigma_m: 5000 }))).toBe(UNCERTAINTY_MAX_RADIUS_M);
  });
});
