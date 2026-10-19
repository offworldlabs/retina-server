import { describe, it, expect } from "vitest";
import bar from "./ArchiveBar.css?raw";
import map from "./LiveAircraftMap.css?raw";

/** A rule's declarations, found by its exact selector line. */
function body(css: string, selector: string): string {
  const at = css.indexOf(`${selector} {`);
  expect(at, `${selector} is missing`).toBeGreaterThanOrEqual(0);
  return css.slice(at, css.indexOf("}", at));
}

describe("the archive bar on a phone", () => {
  const start = bar.indexOf("@media (max-width: 600px)");
  const phone = bar.slice(start);

  it("has a phone block", () => {
    expect(start).toBeGreaterThanOrEqual(0);
  });

  it("caps the bar at half the screen and scrolls it", () => {
    const rule = body(phone, "  .archive-bar");
    expect(rule).toMatch(/max-height:\s*50vh/);
    expect(rule).toMatch(/overflow-y:\s*auto/);
  });

  it("wraps the main row", () => {
    expect(bar).toMatch(/\.archive-row-main\s*\{[^}]*flex-wrap:\s*wrap/);
  });

  it("keeps touch targets at 36px", () => {
    expect(body(phone, "  .archive-bar .btn")).toMatch(/min-height:\s*36px/);
  });
});

describe("the map around the archive bar", () => {
  it("lifts the legend over the bar on desktop", () => {
    expect(body(map, ".map-legend.with-archive")).toMatch(
      /bottom:\s*calc\(var\(--archive-bar-h,\s*183px\)\s*\+\s*9px\)/,
    );
  });

  it("hides that legend on a phone", () => {
    const phone = map.slice(map.indexOf("@media (max-width: 600px)"));
    expect(body(phone, "  .map-legend.with-archive")).toMatch(/display:\s*none/);
  });

  it("styles the archive pill without an animation", () => {
    const selector = ".app.map-surface .badge.connection-badge.archive";
    expect(body(map, selector)).toMatch(/color:\s*var\(--violet\)/);
    expect(body(map, selector)).not.toMatch(/animation/);
    expect(body(map, `${selector}::before`)).not.toMatch(/animation/);
  });
});
