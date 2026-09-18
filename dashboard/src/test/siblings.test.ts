import { describe, it, expect } from "vitest";
import { towerFinderUrl } from "../utils/siblings";

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
