import { describe, it, expect } from "vitest";
import { forgetTrack, reconcileAdsbPairs, snapTrack, sweepStaleRadar } from "./trackStores";

function stores() {
  return {
    fixes: {}, smooth: {}, svgElems: {}, svgMiss: {}, latLng: {},
    trails: {}, lastTrailSample: {},
    markerRegistry: new Map(),
  };
}

describe("forgetTrack", () => {
  it("removes the key from every store, including the marker registry", () => {
    const s: any = stores();
    for (const k of ["fixes", "smooth", "svgElems", "svgMiss", "latLng", "trails", "lastTrailSample"]) {
      s[k]["abc"] = { any: 1 };
      s[k]["keep"] = { any: 2 };
    }
    s.markerRegistry.set("abc", {});
    forgetTrack("abc", s);
    for (const k of ["fixes", "smooth", "svgElems", "svgMiss", "latLng", "trails", "lastTrailSample"]) {
      expect(s[k]["abc"]).toBeUndefined();
      expect(s[k]["keep"]).toBeDefined();
    }
    expect(s.markerRegistry.has("abc")).toBe(false);
  });
});

describe("snapTrack", () => {
  it("mutates smooth and latLng in place and drops the trail buffers", () => {
    const s: any = stores();
    const sm = { lat: 1, lon: 2, track: 90 };
    const ll = { lat: 1, lng: 2 };
    s.smooth.abc = sm;
    s.latLng.abc = ll;
    s.trails.abc = [[1, 2, 3]];
    s.lastTrailSample.abc = 123;
    snapTrack("abc", s, 34.8, -82.4, 180);
    expect(s.smooth.abc).toBe(sm);        // same object — the loop holds it
    expect(sm.lat).toBe(34.8);
    expect(ll.lat).toBe(34.8);
    expect(ll.lng).toBe(-82.4);
    expect(s.trails.abc).toBeUndefined();
    expect(s.lastTrailSample.abc).toBeUndefined();
  });

  it("creates the smooth entry when absent", () => {
    const s: any = stores();
    snapTrack("abc", s, 34.8, -82.4, 45);
    expect(s.smooth.abc).toEqual({ lat: 34.8, lon: -82.4, track: 45 });
  });
});

describe("reconcileAdsbPairs", () => {
  const NOW = 1_000_000;
  // The store shape the ingest effect leaves behind: `hex` is the store key,
  // _updatedAt the ingest wall clock, `seen` the backend's age-of-solve.
  const mnFix = (adsbHex: string, seen: number, updatedAt = NOW) => ({
    position_source: "multinode_solve", adsb_hex: adsbHex, seen, _updatedAt: updatedAt,
  });
  const singleFix = (seen: number, updatedAt = NOW) => ({
    position_source: "adsb_single_node", seen, _updatedAt: updatedAt,
  });
  const mnEntry = (hex: string, adsbHex: string, seen: number) => ({
    hex, position_source: "multinode_solve", adsb_hex: adsbHex, seen,
  });

  it("forgets a lingering single-node fix when the aircraft goes multi-node", () => {
    const s: any = stores();
    // The ICAO-keyed fix stopped arriving 5 s ago; the mn entry is in this frame.
    s.fixes["abc123"] = singleFix(1, NOW - 5_000);
    s.smooth["abc123"] = {};
    s.fixes["mnaaaa"] = mnFix("abc123", 0.5);
    reconcileAdsbPairs([mnEntry("mnaaaa", "abc123", 0.5)], s, NOW);
    expect(s.fixes["abc123"]).toBeUndefined();
    expect(s.smooth["abc123"]).toBeUndefined();
    expect(s.fixes["mnaaaa"]).toBeDefined();
  });

  it("forgets the multi-node side when the single-node solve is fresher", () => {
    const s: any = stores();
    // Divergence: both in frame, but the mn entry is 40 s of dead reckoning.
    s.fixes["abc123"] = singleFix(1);
    s.fixes["mnaaaa"] = mnFix("abc123", 40);
    reconcileAdsbPairs([mnEntry("mnaaaa", "abc123", 40), { hex: "abc123", position_source: "adsb_single_node", seen: 1 }], s, NOW);
    expect(s.fixes["mnaaaa"]).toBeUndefined();
    expect(s.fixes["abc123"]).toBeDefined();
  });

  it("keeps the multi-node side on a tie", () => {
    const s: any = stores();
    s.fixes["abc123"] = singleFix(2);
    s.fixes["mnaaaa"] = mnFix("abc123", 2);
    reconcileAdsbPairs([mnEntry("mnaaaa", "abc123", 2)], s, NOW);
    expect(s.fixes["mnaaaa"]).toBeDefined();
    expect(s.fixes["abc123"]).toBeUndefined();
  });

  it("forgets an mn loser left over from an earlier frame", () => {
    const s: any = stores();
    // single→multi→single flap: the mn side stopped arriving 6 s ago and is
    // not in this frame at all, so only the store scan can find it.
    s.fixes["mnaaaa"] = mnFix("abc123", 1, NOW - 6_000);
    s.fixes["abc123"] = singleFix(0.5);
    reconcileAdsbPairs([{ hex: "abc123", position_source: "adsb_single_node", seen: 0.5 }], s, NOW);
    expect(s.fixes["mnaaaa"]).toBeUndefined();
    expect(s.fixes["abc123"]).toBeDefined();
  });

  it("leaves dark, arc-only, truth and unpaired entries alone", () => {
    const s: any = stores();
    s.fixes["mndark"] = { position_source: "multinode_solve", seen: 40, _updatedAt: NOW };
    s.fixes["arc1"] = { position_source: "single_node_ellipse_arc", seen: 40, _updatedAt: NOW };
    s.fixes["gt:abc123"] = { position_source: "multinode_solve", adsb_hex: "abc123", _isTruth: true, _updatedAt: 0 };
    // Paired hex present, but the other side is not a single-node ADS-B entry.
    s.fixes["deadbe"] = { position_source: "solver_adsb_seed", seen: 30, _updatedAt: NOW };
    s.fixes["mnbbbb"] = mnFix("deadbe", 1);
    reconcileAdsbPairs([mnEntry("mnbbbb", "deadbe", 1)], s, NOW);
    for (const k of ["mndark", "arc1", "gt:abc123", "deadbe", "mnbbbb"]) {
      expect(s.fixes[k]).toBeDefined();
    }
  });

  it("does nothing when no multinode fix carries an adsb_hex", () => {
    const s: any = stores();
    s.fixes["abc123"] = singleFix(1);
    s.fixes["mndark"] = { position_source: "multinode_solve", seen: 1, _updatedAt: NOW };
    reconcileAdsbPairs([], s, NOW);
    expect(Object.keys(s.fixes).sort()).toEqual(["abc123", "mndark"]);
  });
});

describe("sweepStaleRadar", () => {
  it("forgets stale radar keys but never truth keys", () => {
    const s: any = stores();
    s.fixes.old = { _updatedAt: 0 };
    s.fixes.fresh = { _updatedAt: 999_000 };
    s.fixes["gt:x"] = { _updatedAt: 0, _isTruth: true };
    s.smooth.old = {};
    sweepStaleRadar(s, 1_000_000, 8000);
    expect(s.fixes.old).toBeUndefined();
    expect(s.smooth.old).toBeUndefined();
    expect(s.fixes.fresh).toBeDefined();
    expect(s.fixes["gt:x"]).toBeDefined();
  });
});
