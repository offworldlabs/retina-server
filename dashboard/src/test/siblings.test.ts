import { describe, it, expect } from "vitest";
import { mapUrl, towerFinderUrl } from "../utils/siblings";

describe("mapUrl", () => {
  it("stays on this origin under the /dash/ mount", () => {
    for (const host of ["app.retina.fm", "staging-app.retina.fm", "test-app.retina.fm"]) {
      expect(mapUrl(host, "https:", "/dash"), host).toBe("/");
    }
  });

  it("names the app host of this environment from the admin vhost", () => {
    expect(mapUrl("admin.retina.fm", "https:", "")).toBe("https://app.retina.fm/");
    expect(mapUrl("staging-admin.retina.fm", "https:", "")).toBe("https://staging-app.retina.fm/");
    expect(mapUrl("test-admin.retina.fm", "https:", "")).toBe("https://test-app.retina.fm/");
  });

  // The bug this file exists to prevent: a staging page linking to production.
  it("never leaves the environment", () => {
    for (const host of ["staging-admin.retina.fm", "test-admin.retina.fm"]) {
      expect(mapUrl(host, "https:", ""), host).not.toContain("//app.retina.fm");
    }
  });

  it("falls back to production where the host names no environment", () => {
    expect(mapUrl("localhost:5174", "http:", "")).toBe("https://app.retina.fm/");
    expect(mapUrl("192.168.1.9:5174", "http:", "")).toBe("https://app.retina.fm/");
  });

  it("keeps the port of the laptop stack's admin vhost", () => {
    expect(mapUrl("admin.localhost:8080", "http:", "")).toBe("http://app.localhost:8080/");
  });
});

describe("towerFinderUrl", () => {
  it("is absolute from either surface, and stays in the environment", () => {
    expect(towerFinderUrl("app.retina.fm", "https:")).toBe("https://towers.retina.fm/");
    expect(towerFinderUrl("staging-app.retina.fm", "https:")).toBe(
      "https://staging-towers.retina.fm/",
    );
    expect(towerFinderUrl("test-admin.retina.fm", "https:")).toBe(
      "https://test-towers.retina.fm/",
    );
  });

  // The laptop stack reaches every vhost on one published port, so a sibling
  // named without it is an origin nothing is listening on.
  it("keeps the scheme and the port of the page it is linked from", () => {
    expect(towerFinderUrl("app.localhost:8080", "http:")).toBe("http://towers.localhost:8080/");
  });

  it("falls back to production from a dev server with no surface in its name", () => {
    expect(towerFinderUrl("localhost:5174", "http:")).toBe("https://towers.retina.fm/");
  });
});
