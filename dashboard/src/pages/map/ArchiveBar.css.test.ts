import { describe, it, expect } from "vitest";
import bar from "./ArchiveBar.css?raw";
import map from "./LiveAircraftMap.css?raw";
import strip from "./AvailabilityStrip.tsx?raw";
import touchFloor from "../../styles/phone.css?raw";

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

const PHONE = "(max-width: 640px)";
const TABLET = "(max-width: 1023.98px)";
const TOUCH = "(max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";

/** Where the brace opening at `open` closes. */
function closing(css: string, open: number): number {
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}" && --depth === 0) return i;
  }
  throw new Error(`unbalanced braces after ${open}`);
}

/** The rules of the @media block with exactly this query, comments dropped. */
function media(css: string, query: string): string {
  const text = bare(css);
  const at = text.indexOf(`@media ${query} {`);
  expect(at, `@media ${query} is missing`).toBeGreaterThanOrEqual(0);
  const open = text.indexOf("{", at);
  return text.slice(open + 1, closing(text, open));
}

/** The stylesheet as a desktop draws it: no comments and no @media blocks. */
function desktopOnly(css: string): string {
  let text = bare(css);
  for (let at = text.indexOf("@media"); at >= 0; at = text.indexOf("@media")) {
    text = text.slice(0, at) + text.slice(closing(text, text.indexOf("{", at)) + 1);
  }
  return text;
}

/** One rule's declarations, by property; a property declared twice keeps both values, in order. */
function decls(css: string, selector: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const part of body(css, selector).slice(selector.length + 2).split(";")) {
    const colon = part.indexOf(":");
    if (colon > 0) (out[part.slice(0, colon).trim()] ??= []).push(part.slice(colon + 1).trim());
  }
  return out;
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
    const phone = media(bar, PHONE);
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
  // Read inside each test, so a missing block fails it rather than the file.
  const phone = () => media(bar, PHONE);

  it("switches at the console's phone width, and nowhere else", () => {
    expect(bar).not.toMatch(/max-width:\s*600px/);
  });

  it("wraps the main row", () => {
    expect(bar).toMatch(/\.archive-row-main\s*\{[^}]*flex-wrap:\s*wrap/);
  });

  it("stacks the field over its local reading and one row of playback, rows 4px apart", () => {
    expect(decls(phone(), ".archive-bar").gap).toEqual(["4px"]);
    expect(decls(phone(), ".archive-bar")["max-height"]).toBeUndefined();
    expect(decls(phone(), ".archive-row-main").gap).toEqual(["4px 6px"]);
    expect(decls(phone(), ".archive-time").flex).toEqual(["1 1 100%"]);
    expect(decls(phone(), ".archive-local")).toMatchObject({
      flex: ["1 1 100%"],
      "font-size": ["11px"],
      "line-height": ["14px"],
    });
    // Now ends the playback row and takes what it leaves, rather than a row of its own.
    expect(decls(phone(), ".archive-bar .archive-jump")).toEqual({ "margin-left": ["0"], flex: ["1 1 auto"] });
  });

  it("takes the folded strip out of the flow, so no gap is left for it", () => {
    expect(decls(phone(), ".archive-strip[hidden]")).toEqual({ display: ["none"] });
  });

  it("points the strip toggle's chevron up while folded and down while open", () => {
    expect(decls(phone(), ".archive-hours-toggle::after").transform).toEqual(["translateY(2px) rotate(45deg)"]);
    expect(decls(phone(), '.archive-hours-toggle[aria-expanded="true"]::after').transform).toEqual([
      "translateY(-2px) rotate(225deg)",
    ]);
  });

  it("sets no button or field height below a 44px target", () => {
    for (const block of [phone(), media(bar, TOUCH)]) {
      for (const [, px] of block.matchAll(/min-height:\s*(\d+)px/g)) expect(Number(px)).toBeGreaterThanOrEqual(44);
    }
  });
});

describe("the archive bar under touch", () => {
  const touch = () => media(bar, TOUCH);

  it("switches where styles/phone.css raises its targets to 44px", () => {
    expect(bare(touchFloor)).toContain(`@media ${TOUCH} {`);
  });

  it("gives play and the rates a 44px target's width, which their own 36px would outrank", () => {
    expect(decls(touch(), ".archive-bar .archive-play,\n  .archive-bar .archive-rate-btn")).toEqual({ "min-width": ["44px"] });
  });

  it("makes the scrubber a 44px target", () => {
    expect(decls(touch(), ".archive-scrubber")).toEqual({ height: ["44px"] });
  });

  it("keeps the time field at 16px so iOS does not zoom on focus", () => {
    expect(decls(touch(), ".archive-time-input")["font-size"]).toEqual(["16px"]);
  });

  it("sets the rates side by side, so no rate's edge lies under its neighbour, one line still between them", () => {
    expect(decls(desktopOnly(bar), ".archive-bar .archive-rate-btn + .archive-rate-btn")).toEqual({ "margin-left": ["-1px"] });
    expect(decls(touch(), ".archive-bar .archive-rate-btn + .archive-rate-btn")).toEqual({
      "margin-left": ["0"],
      "border-left-width": ["0"],
    });
    expect(
      decls(
        touch(),
        '.archive-bar .archive-rate-btn + .archive-rate-btn[aria-pressed="true"],\n  .archive-bar .archive-rate-btn + .archive-rate-btn:hover',
      ),
    ).toEqual({ "box-shadow": ["-1px 0 0 var(--accent)"] });
  });
});

describe("the archive bar below desktop width", () => {
  it("caps the bar at half the screen in dvh, after a vh fallback, and scrolls it", () => {
    expect(decls(media(bar, TABLET), ".archive-bar")).toMatchObject({
      "max-height": ["50vh", "50dvh"],
      "overflow-y": ["auto"],
    });
  });

  it("pads its foot by the home indicator's inset", () => {
    expect(decls(media(bar, TABLET), ".archive-bar")["padding-bottom"]).toEqual([
      "calc(var(--archive-bar-pad-y) + env(safe-area-inset-bottom, 0px))",
    ]);
  });

  it("keeps its right end clear of the side inset, and its left end on a phone", () => {
    const tablet = decls(media(bar, TABLET), ".archive-bar");
    expect(tablet).toMatchObject({ "padding-right": ["max(14px, env(safe-area-inset-right, 0px))"] });
    expect(tablet["padding-left"]).toBeUndefined();
    expect(decls(media(bar, PHONE), ".archive-bar")).toMatchObject({
      "padding-right": ["max(10px, env(safe-area-inset-right, 0px))"],
      "padding-left": ["max(10px, env(safe-area-inset-left, 0px))"],
    });
  });

  it("keeps that inset on a phone, whose padding sets the sides only", () => {
    const sides = Object.keys(decls(media(bar, PHONE), ".archive-bar")).filter((p) => p.startsWith("padding"));
    expect(sides).toEqual(["padding-right", "padding-left"]);
  });
});

describe("the strip's unit band", () => {
  const phone = () => rules(media(bar, PHONE));
  const desktop = rules(desktopOnly(bar));
  const label = ".edsc-timeline-interval__interval-label";
  const minute = '.archive-strip-host .edsc-timeline:has([data-testid="zoomDown"]:disabled)';
  const day = '.archive-strip-host .edsc-timeline:has([data-testid="zoomUp"]:disabled)';
  const shown = () => phone().filter((r) => /display: inline;/.test(r.decls)).map((r) => r.selector);

  it("hides the unit labels on a phone and passes taps through them to the unit underneath", () => {
    const hidden = phone().find((r) => r.selector === `.archive-strip-host ${label}`);
    expect(hidden?.decls).toMatch(/display: none;/);
    expect(hidden?.decls).toMatch(/pointer-events: none;/);
  });

  it("labels every 15th minute and every 5th day", () => {
    expect(shown()).toHaveLength(1);
    const [, minutes, days] = split(shown()[0]);
    expect(minutes).toBe(`${minute} .edsc-timeline-interval:nth-child(15n+1) ${label}`);
    expect(days).toBe(`${day} .edsc-timeline-interval:nth-child(5n+1) ${label}`);
  });

  it("keeps a dot on every fifth minute only, on a phone", () => {
    const dots = phone().find((r) => r.selector === `${minute} .edsc-timeline-interval:not(:nth-child(5n+1))::after`);
    expect(dots?.decls).toMatch(/display: none;/);
  });

  it("leaves the desktop band's labels and dots as the timeline draws them", () => {
    const band = desktop.filter((r) => /interval__interval-label|interval:{1,2}after/.test(r.selector));
    for (const r of band) expect(r.decls).not.toMatch(/display|pointer-events|content/);
  });

  it("scopes every rule on the timeline to the strip, so the Data Explorer's is untouched", () => {
    const timeline = [...desktop, ...phone()].flatMap((r) => split(r.selector)).filter((s) => s.includes("edsc-"));
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

  it("styles the archive pill without an animation", () => {
    const selector = ".app.map-surface .badge.connection-badge.archive";
    expect(body(map, selector)).toMatch(/color:\s*var\(--violet\)/);
    expect(body(map, selector)).not.toMatch(/animation/);
    expect(body(map, `${selector}::before`)).not.toMatch(/animation/);
  });
});
