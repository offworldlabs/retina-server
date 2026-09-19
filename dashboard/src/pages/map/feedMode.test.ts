import { describe, it, expect, vi, afterEach } from "vitest";

// defaultFeedMode reads the predicates in utils/domains, and those capture the
// hostname once at module load, so a hostname can only be varied by re-importing
// the pair: resetModules drops both cached copies and the stubbed window is what
// the fresh ones read. Same pattern, and the same reason, as domains.test.ts.
async function loadFor(hostname: string) {
  vi.resetModules();
  vi.stubGlobal("window", { location: { hostname } });
  return import("./feedMode");
}

afterEach(() => vi.unstubAllGlobals());

// Only /map takes this answer. /sim passes "synthetic" explicitly, so none of
// these hosts can put the real fleet on the simulator's page — and no host
// puts the synthetic fleet on /map, staging included.
describe("the fleet /map defaults to", () => {
  it("is the real one on every deployed environment", async () => {
    for (const host of ["app.retina.fm", "staging-app.retina.fm", "test-app.retina.fm"]) {
      const m = await loadFor(host);
      expect(m.defaultFeedMode(), host).toBe("real");
    }
  });

  it("is both fleets on the laptop", async () => {
    const m = await loadFor("app.localhost");
    expect(m.defaultFeedMode()).toBe("all");
  });

  // A host the map is not served from has no fleet of its own to name. "all" is
  // the only reading that cannot empty a map: the real-only feed carries no
  // synthetic fleet and the synthetic filter drops every real node, so either of
  // the narrow modes would be a guess that shows nothing.
  it("is both fleets on a host that is not a map surface", async () => {
    for (const host of ["dash.retina.fm", "localhost"]) {
      const m = await loadFor(host);
      expect(m.defaultFeedMode(), host).toBe("all");
    }
  });
});
