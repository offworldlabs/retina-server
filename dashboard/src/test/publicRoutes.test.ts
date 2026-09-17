import { describe, it, expect } from "vitest";

import { PUBLIC_PATHS, isPublicRoute } from "../utils/publicRoutes";

describe("the routes a visitor reaches without signing in", () => {
  it.each(PUBLIC_PATHS)("admits %s on the user surface", (path) => {
    expect(isPublicRoute(path, false)).toBe(true);
  });

  it("admits a deeper path under a public one", () => {
    expect(isPublicRoute("/data/2026/09/17", false)).toBe(true);
  });

  it("admits a public path spelled with a trailing slash", () => {
    expect(isPublicRoute("/leaderboard/", false)).toBe(true);
  });

  // The match is per segment, not per prefix: a route added later whose name
  // merely starts with a public one must not inherit its openness.
  it("refuses a path that only starts like a public one", () => {
    expect(isPublicRoute("/datasets", false)).toBe(false);
  });

  it("refuses a route that is not on the list", () => {
    expect(isPublicRoute("/settings", false)).toBe(false);
  });

  // Overview, which reports the caller's own nodes.
  it("refuses the index", () => {
    expect(isPublicRoute("/", false)).toBe(false);
  });

  it("refuses every path on the admin surface", () => {
    for (const path of PUBLIC_PATHS) expect(isPublicRoute(path, true)).toBe(false);
  });
});
