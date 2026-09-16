import { describe, it, expect, vi, afterEach } from "vitest";

/**
 * The mount prefix is baked in by Vite's `base` at build time, so a bundle can
 * only be examined at the other mount by re-importing it under a stubbed one:
 * resetModules drops the cached copy and the fresh one reads the stub.
 */
async function loadFor(base: string) {
  vi.resetModules();
  vi.stubEnv("BASE_URL", base);
  return import("../utils/basePath");
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("the mount this bundle was built for", () => {
  it("is empty at a vhost root, so in-app paths are unchanged", async () => {
    const m = await loadFor("/");
    expect(m.BASE_PATH).toBe("");
    expect(m.withBase("/login")).toBe("/login");
  });

  it("is the prefix under the app vhost's mount", async () => {
    const m = await loadFor("/dash/");
    expect(m.BASE_PATH).toBe("/dash");
    expect(m.withBase("/login")).toBe("/dash/login");
    // The dashboard's own root, which is what the OAuth return address needs.
    expect(m.withBase("/")).toBe("/dash/");
  });

  // react-router rejects "" and wants "/" for a bundle served at a root.
  it("hands react-router a basename it accepts at either mount", async () => {
    expect((await loadFor("/")).ROUTER_BASENAME).toBe("/");
    expect((await loadFor("/dash/")).ROUTER_BASENAME).toBe("/dash");
  });
});
