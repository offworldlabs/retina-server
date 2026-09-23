import { describe, it, expect, vi, afterEach } from "vitest";

// HOSTNAME is captured at module load, so a hostname can only be varied by
// re-importing: resetModules drops the cached copy and the stubbed window is
// what the fresh one reads.
async function loadFor(hostname: string) {
  vi.resetModules();
  vi.stubGlobal("window", { location: { hostname } });
  return import("../utils/domains");
}

afterEach(() => vi.unstubAllGlobals());

// The hostname's answer is /map's default and nothing more: /sim asks for the
// synthetic fleet whatever the host says (feedMode.test.ts).
describe("the fleet each host's /map runs", () => {
  it("every deployed environment's /map runs the real fleet", async () => {
    for (const host of ["app.retina.fm", "staging-app.retina.fm", "test-app.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(true);
    }
  });

  it("the laptop runs both fleets", async () => {
    const m = await loadFor("app.localhost");
    expect(m.usesRealOnlyFeed).toBe(false);
  });

  // The retired names, which Cloudflare redirects to the app host. A bundle
  // loaded under one of them would mean a redirect had been dropped, so none of
  // them may quietly keep working as a real-fleet map.
  it("the retired hostnames are not map hosts", async () => {
    for (const host of [
      "map.retina.fm", "staging-map.retina.fm", "test-map.retina.fm",
      "dash.retina.fm", "data.retina.fm",
    ]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(false);
    }
  });

  it("the tower-search hosts are not map hosts", async () => {
    for (const host of ["towers.retina.fm", "staging-towers.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.usesRealOnlyFeed, host).toBe(false);
    }
  });
});
