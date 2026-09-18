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

// These are the hostname's answer, which is /map's default and nothing more:
// /sim asks for the synthetic fleet whatever the host says (feedMode.test.ts).
describe("the fleet each surface runs", () => {
  it("the real-radar surfaces run the real fleet", async () => {
    for (const host of ["app.retina.fm", "test-app.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(true);
      expect(m.defaultsGroundTruthOff, host).toBe(true);
      expect(m.hidesRealNodes, host).toBe(false);
    }
  });

  it("the public demo runs the synthetic fleet", async () => {
    const m = await loadFor("staging-app.retina.fm");
    expect(m.usesRealOnlyFeed).toBe(false);
    expect(m.defaultsGroundTruthOff).toBe(false);
    expect(m.hidesRealNodes).toBe(true);
  });

  it("the laptop runs both fleets", async () => {
    const m = await loadFor("app.localhost");
    expect(m.usesRealOnlyFeed).toBe(false);
    expect(m.hidesRealNodes).toBe(false);
  });

  it("every map surface still defaults to the Live Radar tab", async () => {
    for (const host of [
      "app.retina.fm", "staging-app.retina.fm", "test-app.retina.fm",
      "app.localhost",
    ]) {
      const m = await loadFor(host);
      expect(m.isMapDomain, host).toBe(true);
    }
  });

  // The retired names, which Cloudflare now redirects to the app host. A bundle
  // loaded under one of them would mean a redirect had been dropped, so none of
  // them may quietly keep working as a map surface.
  it("the retired hostnames are not map surfaces", async () => {
    for (const host of [
      "map.retina.fm", "testmap.retina.fm", "staging-map.retina.fm",
      "test-map.retina.fm", "test-testmap.retina.fm", "dash.retina.fm",
      "data.retina.fm",
    ]) {
      const m = await loadFor(host);
      expect(m.isMapDomain, host).toBe(false);
      expect(m.usesRealOnlyFeed, host).toBe(false);
      expect(m.hidesRealNodes, host).toBe(false);
    }
  });

  it("the tower-search surfaces are none of these", async () => {
    for (const host of ["towers.retina.fm", "staging-towers.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.isMapDomain, host).toBe(false);
      expect(m.usesRealOnlyFeed, host).toBe(false);
      expect(m.hidesRealNodes, host).toBe(false);
    }
  });
});
