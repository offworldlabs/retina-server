import { describe, it, expect } from "vitest";
import bar from "./ArchiveBar.css?raw";
import map from "./LiveAircraftMap.css?raw";
import strip from "./AvailabilityStrip.tsx?raw";

/** A rule's declarations, found by its exact selector line. */
function body(css: string, selector: string): string {
  const at = css.indexOf(`${selector} {`);
  expect(at, `${selector} is missing`).toBeGreaterThanOrEqual(0);
  return css.slice(at, css.indexOf("}", at));
}

/** The stylesheet without its comments. */
function bare(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** Every value a custom property is declared with, whitespace runs made one space. */
function declared(css: string, name: string): string[] {
  return [...bare(css).matchAll(new RegExp(`(?<![\\w-])${name}\\s*:\\s*([^;]+);`, "g"))].map(([, value]) =>
    value.replace(/\s+/g, " ").trim(),
  );
}

/** Each rule as its selector and declarations, comments dropped and runs of
 *  whitespace made one space. At-rule preludes are skipped. */
function rules(css: string): { selector: string; decls: string }[] {
  const flat = css.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ");
  return [...flat.matchAll(/([^{}@]+)\{([^{}]*)\}/g)].map(([, selector, decls]) => ({
    selector: selector.trim(),
    decls,
  }));
}

/** A selector list split at its top-level commas. */
function split(list: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < list.length; i++) {
    if (list[i] === "(") depth++;
    else if (list[i] === ")") depth--;
    else if (list[i] === "," && depth === 0) {
      out.push(list.slice(start, i).trim());
      start = i + 1;
    }
  }
  return [...out, list.slice(start).trim()];
}

describe("the availability strip", () => {
  // A height declaration; min-height and max-height do not match.
  const height = /(^|[^-\w])height\s*:/m;

  it("grows with the rows its timeline draws rather than holding a fixed height", () => {
    const host = body(bar, ".archive-strip-host");
    expect(host).not.toMatch(height);
    expect(host).not.toMatch(/max-height/);
  });

  it("holds no fixed height on a phone either", () => {
    const phone = bar.slice(bar.indexOf("@media (max-width: 600px)"));
    const at = phone.indexOf(".archive-strip-host {");
    if (at >= 0) expect(phone.slice(at, phone.indexOf("}", at))).not.toMatch(height);
  });

  it("keeps the one-row strip's height while the timeline loads or after it fails", () => {
    expect(body(bar, ".archive-strip-host")).toMatch(/min-height:\s*var\(--archive-strip-min-h\)/);
    expect(declared(bar, "--archive-strip-min-h")).toEqual([
      "calc(0.925rem + var(--archive-strip-row-h) + 1.575rem + 4px)",
    ]);
  });
});

describe("the archive bar's measures", () => {
  const rootBlock = /:root\s*\{([^}]*)\}/;
  const root = bare(bar).match(rootBlock)?.[1] ?? "";
  const names = [...root.matchAll(/(--[\w-]+)\s*:/g)].map(([, name]) => name);
  // The bar's own rules, without the block that declares the measures.
  const drawn = bare(bar).replace(rootBlock, "");

  it("declares each one once, on :root in ArchiveBar.css, the legend's included", () => {
    expect(names).toEqual(
      expect.arrayContaining(["--archive-bar-one-row-h", "--archive-strip-row-h", "--archive-legend-gap"]),
    );
    for (const name of names) {
      expect(name.startsWith("--archive-"), name).toBe(true);
      expect([...declared(bar, name), ...declared(map, name)], name).toHaveLength(1);
    }
  });

  it("reads every one it declares", () => {
    const readers = `${root}${drawn}${bare(map)}`;
    for (const name of names) expect(readers, name).toContain(`var(${name})`);
  });

  it("adds the one-row height up from the measures the bar's rules draw with, and no length of its own", () => {
    const [sum] = declared(bar, "--archive-bar-one-row-h");
    const terms = [...sum.matchAll(/var\((--[\w-]+)\)/g)].map(([, name]) => name);
    expect(terms.length).toBeGreaterThan(1);
    // Bare numbers are counts (two paddings, three gaps); a unit would be a length.
    expect(sum.replace(/var\(--[\w-]+\)/g, "")).not.toMatch(/\d\s*[a-z%]/i);
    for (const term of terms) expect(drawn, term).toContain(`var(${term})`);
  });
});

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

describe("the strip's unit band", () => {
  const start = bar.indexOf("@media (max-width: 600px)");
  const phone = rules(bar.slice(start));
  const desktop = rules(bar.slice(0, start));
  const label = ".edsc-timeline-interval__interval-label";
  const minute = '.archive-strip-host .edsc-timeline:has([data-testid="zoomDown"]:disabled)';
  const day = '.archive-strip-host .edsc-timeline:has([data-testid="zoomUp"]:disabled)';
  const shown = phone.filter((r) => /display: inline;/.test(r.decls)).map((r) => r.selector);

  it("hides the unit labels on a phone and passes taps through them to the unit underneath", () => {
    const hidden = phone.find((r) => r.selector === `.archive-strip-host ${label}`);
    expect(hidden?.decls).toMatch(/display: none;/);
    expect(hidden?.decls).toMatch(/pointer-events: none;/);
  });

  it("labels every 15th minute and every 5th day", () => {
    expect(shown).toHaveLength(1);
    const [, minutes, days] = split(shown[0]);
    expect(minutes).toBe(`${minute} .edsc-timeline-interval:nth-child(15n+1) ${label}`);
    expect(days).toBe(`${day} .edsc-timeline-interval:nth-child(5n+1) ${label}`);
  });

  it("keeps a dot on every fifth minute only, on a phone", () => {
    const dots = phone.find((r) => r.selector === `${minute} .edsc-timeline-interval:not(:nth-child(5n+1))::after`);
    expect(dots?.decls).toMatch(/display: none;/);
  });

  it("leaves the desktop band's labels and dots as the timeline draws them", () => {
    const band = desktop.filter((r) => /interval__interval-label|interval:{1,2}after/.test(r.selector));
    for (const r of band) expect(r.decls).not.toMatch(/display|pointer-events|content/);
  });

  it("scopes every rule on the timeline to the strip, so the Data Explorer's is untouched", () => {
    const timeline = [...desktop, ...phone].flatMap((r) => split(r.selector)).filter((s) => s.includes("edsc-"));
    expect(timeline.length).toBeGreaterThan(0);
    for (const s of timeline) expect(s.startsWith(".archive-strip-host ")).toBe(true);
  });

  it("takes the minute and day views from the zoom buttons at the strip's bounds", () => {
    expect(strip).toMatch(/const MIN_ZOOM = 0;/);
    expect(strip).toMatch(/const MAX_ZOOM = 2;/);
  });
});

describe("the map around the archive bar", () => {
  it("lifts the legend over the bar on desktop, by the height the bar publishes or else its one-row height", () => {
    expect(body(map, ".map-legend.with-archive")).toMatch(
      /bottom:\s*calc\(var\(--archive-bar-h,\s*var\(--archive-bar-one-row-h\)\)\s*\+\s*var\(--archive-legend-gap\)\)/,
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
