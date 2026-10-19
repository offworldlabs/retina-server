import { describe, it, expect } from "vitest";
import mapCss from "./LiveAircraftMap.css?raw";
import barCss from "./ArchiveBar.css?raw";

type Decls = Record<string, string>;
interface Sheet {
  /** Rules outside any at-rule, by selector. */
  top: Map<string, Decls>;
  /** Rules inside each @media block, by its query, then by selector. */
  media: Map<string, Map<string, Decls>>;
}

/** The text between the brace that opens after `from` and its partner. */
function braced(css: string, from: number): { inner: string; end: number } {
  const open = css.indexOf("{", from);
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth++;
    else if (css[i] === "}" && --depth === 0) return { inner: css.slice(open + 1, i), end: i + 1 };
  }
  throw new Error(`unbalanced braces after ${from}`);
}

function declarations(body: string): Decls {
  const out: Decls = {};
  for (const part of body.split(";")) {
    const colon = part.indexOf(":");
    if (colon > 0) out[part.slice(0, colon).trim()] = part.slice(colon + 1).trim();
  }
  return out;
}

/** Plain rules into `into`, one entry per selector of a list. */
function collect(css: string, into: Map<string, Decls>, media?: Map<string, Map<string, Decls>>) {
  let at = 0;
  while (css.indexOf("{", at) >= 0) {
    const prelude = css.slice(at, css.indexOf("{", at)).trim().replace(/\s+/g, " ");
    const { inner, end } = braced(css, at);
    if (prelude.startsWith("@media")) {
      const rules = media?.get(prelude) ?? new Map<string, Decls>();
      collect(inner, rules);
      media?.set(prelude, rules);
    } else if (!prelude.startsWith("@")) {
      for (const selector of prelude.split(",")) {
        const key = selector.trim();
        into.set(key, { ...into.get(key), ...declarations(inner) });
      }
    }
    at = end;
  }
}

function parse(css: string): Sheet {
  const sheet: Sheet = { top: new Map(), media: new Map() };
  collect(css.replace(/\/\*[\s\S]*?\*\//g, ""), sheet.top, sheet.media);
  return sheet;
}

const PHONE = "@media (max-width: 600px)";
const NARROW = "@media (max-width: 900px)";
const map = parse(mapCss);
const bar = parse(barCss);

function rule(rules: Map<string, Decls> | undefined, selector: string): Decls {
  const found = rules?.get(selector);
  expect(found, `${selector} is missing`).toBeDefined();
  return found as Decls;
}

const phone = (selector: string) => rule(map.media.get(PHONE), selector);
const px = (value: string) => Number(value.match(/^(\d+(?:\.\d+)?)px$/)?.[1] ?? NaN);
const z = (decls: Decls) => Number(decls["z-index"]);

describe("the map's layout above a phone's width", () => {
  // Pinned so the phone block cannot leak: these are the rail, panel and bar
  // as the desktop and the 601-900px layouts draw them.
  it("keeps the body a flex row and the map area its positioned column", () => {
    expect(rule(map.top, ".live-map-body").display).toBe("flex");
    expect(rule(map.top, ".live-map-area")).toMatchObject({ flex: "1", position: "relative", overflow: "hidden" });
  });

  it("keeps the side panels at their widths", () => {
    expect(rule(map.top, ".aircraft-list-panel").width).toBe("280px");
    expect(rule(map.top, ".aircraft-list-panel.collapsed").width).toBe("36px");
    expect(rule(map.top, ".detail-panel")).toMatchObject({ width: "300px", height: "100%" });
    expect(rule(map.media.get(NARROW), ".aircraft-list-panel").width).toBe("220px");
    expect(rule(map.media.get(NARROW), ".detail-panel").width).toBe("260px");
  });

  it("keeps the archive bar floating at the foot of the map area", () => {
    expect(rule(bar.top, ".archive-bar")).toMatchObject({ position: "absolute", bottom: "0", left: "0", right: "0" });
  });

  it("lays the body out as a grid nowhere but the phone block", () => {
    const blocks = [map.top, ...[...map.media].filter(([query]) => query !== PHONE).map(([, rules]) => rules)];
    for (const rules of blocks) {
      for (const [selector, decls] of rules) {
        if (/^\.(live-map-body|live-map-area|aircraft-list-panel|detail-panel)\b/.test(selector)) {
          expect(decls.display ?? "", selector).not.toMatch(/grid|contents/);
          expect(decls["grid-area"] ?? decls["grid-row"], selector).toBeUndefined();
        }
      }
    }
  });
});

describe("the map on a phone", () => {
  it("makes the body a two-row grid the map spans, the bar's row at its own height", () => {
    expect(phone(".live-map-body")).toMatchObject({
      position: "relative",
      display: "grid",
      "grid-template-columns": "minmax(0, 1fr)",
      "grid-template-rows": "minmax(96px, 1fr) auto",
    });
    expect(phone(".live-map-area").display).toBe("contents");
    expect(phone(".live-map-area > *")["grid-area"]).toBe("1 / 1 / -1 / -1");
  });

  it.each([".live-map-area > .archive-bar", ".live-map-area > .playback-bar"])(
    "puts %s in the bottom row, in flow",
    (selector) => {
      expect(phone(selector)).toMatchObject({ position: "relative", inset: "auto", "grid-row": "2", "grid-column": "1" });
    },
  );

  it("makes the detail panel a sheet over the top row, above the list and below the toolbar", () => {
    const panel = phone(".live-map-body > .detail-panel");
    expect(panel).toMatchObject({ "grid-area": "1 / 1", width: "auto", height: "auto" });
    // Leaflet floats its controls at 1000, and the bar sits at its z-index.
    expect(z(panel)).toBeGreaterThan(Math.max(1000, z(rule(bar.top, ".archive-bar"))));
    expect(z(panel)).toBeGreaterThan(z(phone(".live-map-body > .aircraft-list-panel")));
    expect(z(panel)).toBeLessThan(z(rule(map.top, ".live-map-toolbar")));
  });

  it("gives the detail panel's close button a 40px target that never shrinks", () => {
    const close = phone(".detail-panel-header .close-btn");
    expect(px(close.width)).toBeGreaterThanOrEqual(40);
    expect(px(close.height)).toBeGreaterThanOrEqual(40);
    expect(close["flex-shrink"]).toBe("0");
    expect(phone(".detail-panel-header h3")).toMatchObject({ "min-width": "0", "text-overflow": "ellipsis" });
  });

  it("makes the open list a scrolling sheet over the top row, over Leaflet's controls", () => {
    const list = phone(".live-map-body > .aircraft-list-panel");
    expect(list).toMatchObject({ "grid-area": "1 / 1", width: "auto", "overflow-y": "auto" });
    expect(z(list)).toBeGreaterThan(1000);
    expect(phone(".aircraft-list-panel .al-header")).toMatchObject({ position: "sticky", top: "0" });
    // Four of the virtual list's 40px rows (ROW_HEIGHT in AircraftListPanel).
    expect(px(phone(".aircraft-list-panel .al-list")["min-height"])).toBe(4 * 40);
    const toggle = phone(".aircraft-list-panel .al-collapse-btn");
    expect(px(toggle.width)).toBeGreaterThanOrEqual(40);
    expect(px(toggle.height)).toBeGreaterThanOrEqual(40);
  });

  it("makes the closed list a card in the map's corner that opens from anywhere on it", () => {
    const chip = phone(".live-map-body > .aircraft-list-panel.collapsed");
    expect(chip).toMatchObject({
      position: "relative",
      "justify-self": "start",
      "align-self": "start",
      width: "auto",
      background: "var(--bg-card)",
      border: "1px solid var(--border)",
      "border-radius": "var(--radius)",
      "box-shadow": "var(--panel-shadow)",
    });
    expect(phone(".aircraft-list-panel.collapsed .al-collapse-btn::after")).toMatchObject({
      content: '""',
      position: "absolute",
      inset: "0",
    });
  });

  it("moves Leaflet's zoom control below the list's card", () => {
    const chip = phone(".live-map-body > .aircraft-list-panel.collapsed");
    const header = phone(".aircraft-list-panel.collapsed .al-header");
    expect(header).toMatchObject({ padding: "0 0 0 12px", "min-height": "0" });
    // The card is the 40px button and its 1px border, inset by its margin.
    const bottom = px(chip.margin) + px(phone(".aircraft-list-panel .al-collapse-btn").height) + 2 * 1;
    expect(px(phone(".live-map-area .leaflet-top.leaflet-left").top)).toBe(bottom);
  });
});
