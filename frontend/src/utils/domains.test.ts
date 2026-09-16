import { describe, it, expect, vi, afterEach } from "vitest";

// HOSTNAME is captured at module load, so a hostname can only be varied by
// re-importing: resetModules drops the cached copy and the stubbed window is
// what the fresh one reads.
async function loadFor(hostname: string) {
  vi.resetModules();
  vi.stubGlobal("window", { location: { hostname } });
  return import("./domains");
}

afterEach(() => vi.unstubAllGlobals());

describe("feed selection by surface", () => {
  it("the real-radar surfaces show real nodes only", async () => {
    for (const host of ["map.retina.fm", "test-map.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(true);
      expect(m.defaultsGroundTruthOff, host).toBe(true);
      expect(m.hidesRealNodes, host).toBe(false);
    }
  });

  // staging-testmap.retina.fm is absent deliberately. It was never a vhost; it
  // reached the SPA only because nginx served unmatched hosts from the first 443
  // block, and the catch-all now 421s it. isMapDomain still matches the name,
  // which costs nothing and keeps the pattern readable.
  it("the public demo shows synthetic nodes only", async () => {
    for (const host of ["testmap.retina.fm", "staging-map.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(false);
      expect(m.hidesRealNodes, host).toBe(true);
    }
  });

  it("the test droplet's synthetic surface is a public demo, since it resolves publicly", async () => {
    const m = await loadFor("test-testmap.retina.fm");
    expect(m.usesRealOnlyFeed).toBe(false);
    expect(m.defaultsGroundTruthOff).toBe(false);
    expect(m.hidesRealNodes).toBe(true);
  });

  it("the laptop shows everything", async () => {
    for (const host of ["map.localhost", "testmap.localhost"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(false);
      expect(m.hidesRealNodes, host).toBe(false);
    }
  });

  it("every map surface still defaults to the Live Radar tab", async () => {
    for (const host of [
      "map.retina.fm", "testmap.retina.fm", "staging-map.retina.fm",
      "test-map.retina.fm", "test-testmap.retina.fm",
      "map.localhost",
    ]) {
      const m = await loadFor(host);
      expect(m.isMapDomain, host).toBe(true);
    }
  });

  it("the tower-search surfaces are none of these", async () => {
    for (const host of ["towers.retina.fm", "staging-towers.retina.fm", "dash.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.isMapDomain, host).toBe(false);
      expect(m.usesRealOnlyFeed, host).toBe(false);
      expect(m.hidesRealNodes, host).toBe(false);
    }
  });
});
