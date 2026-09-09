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
  it("production shows real nodes only", async () => {
    const m = await loadFor("map.retina.fm");
    expect(m.usesRealOnlyFeed).toBe(true);
    expect(m.hidesRealNodes).toBe(false);
  });

  it("the public demo shows synthetic nodes only", async () => {
    for (const host of ["testmap.retina.fm", "staging-testmap.retina.fm", "staging-map.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(false);
      expect(m.hidesRealNodes, host).toBe(true);
    }
  });

  it("the laptop shows everything", async () => {
    for (const host of ["map.localhost", "testmap.localhost"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(false);
      expect(m.hidesRealNodes, host).toBe(false);
    }
  });

  it("the test droplet shows everything", async () => {
    for (const host of ["test-map.retina.fm", "test-testmap.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(false);
      expect(m.hidesRealNodes, host).toBe(false);
    }
  });

  it("every map surface still defaults to the Live Radar tab", async () => {
    for (const host of [
      "map.retina.fm", "testmap.retina.fm", "staging-map.retina.fm",
      "staging-testmap.retina.fm", "test-map.retina.fm", "map.localhost",
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
