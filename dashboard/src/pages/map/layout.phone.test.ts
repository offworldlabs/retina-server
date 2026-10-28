import { describe, it, expect } from "vitest";
import mapCss from "./LiveAircraftMap.css?raw";
import barCss from "./ArchiveBar.css?raw";
import surfaceCss from "./map-surface.css?raw";

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

const PHONE = "@media (max-width: 640px)";
const TABLET = "@media (max-width: 1023.98px)";
const TOUCH = "@media (max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";
const COARSE_TABLET = "@media (max-width: 1023.98px) and (pointer: coarse)";
const DRAWER = "@media (max-width: 768px)";
const TOUCH_TABLET = "@media (min-width: 641px) and (max-width: 1023.98px) and (pointer: coarse)";
const map = parse(mapCss);
const bar = parse(barCss);
const surface = parse(surfaceCss);

function rule(rules: Map<string, Decls> | undefined, selector: string): Decls {
  const found = rules?.get(selector);
  expect(found, `${selector} is missing`).toBeDefined();
  return found as Decls;
}

const phone = (selector: string) => rule(map.media.get(PHONE), selector);
const tablet = (selector: string) => rule(map.media.get(TABLET), selector);
const touch = (selector: string) => rule(map.media.get(TOUCH), selector);
const px = (value: string) => Number(value.match(/^(\d+(?:\.\d+)?)px$/)?.[1] ?? NaN);
const z = (decls: Decls) => Number(decls["z-index"]);
/** Classes and attribute tests in a selector: what decides between two rules
 *  that each name one element. */
const classWeight = (selector: string) => (selector.match(/\.[\w-]+|\[[^\]]*\]/g) ?? []).length;

describe("the map's layout above a phone's width", () => {
  // Pinned so the phone block cannot leak: these are the rail, panel and bar
  // as the desktop and the 641-1023px layouts draw them.
  it("keeps the body a flex row and the map area its positioned column", () => {
    expect(rule(map.top, ".live-map-body").display).toBe("flex");
    expect(rule(map.top, ".live-map-area")).toMatchObject({ flex: "1", position: "relative", overflow: "hidden" });
  });

  it("keeps the side panels at their widths", () => {
    expect(rule(map.top, ".aircraft-list-panel").width).toBe("280px");
    expect(rule(map.top, ".aircraft-list-panel.collapsed").width).toBe("36px");
    expect(rule(map.top, ".detail-panel")).toMatchObject({ width: "300px", height: "100%" });
    expect(tablet(".aircraft-list-panel").width).toBe("220px");
    expect(tablet(".detail-panel").width).toBe("260px");
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

  it("gives the detail panel's close button a 44px target that never shrinks", () => {
    const close = phone(".detail-panel-header .close-btn");
    expect(px(close.width)).toBeGreaterThanOrEqual(44);
    expect(px(close.height)).toBeGreaterThanOrEqual(44);
    expect(close["flex-shrink"]).toBe("0");
    expect(phone(".detail-panel-header h3")).toMatchObject({ "min-width": "0", "text-overflow": "ellipsis" });
  });

  it("makes the open list a scrolling sheet over the top row, over Leaflet's controls", () => {
    const list = phone(".live-map-body > .aircraft-list-panel");
    expect(list).toMatchObject({ "grid-area": "1 / 1", width: "auto", "overflow-y": "auto" });
    expect(z(list)).toBeGreaterThan(1000);
    expect(phone(".aircraft-list-panel .al-header")).toMatchObject({ position: "sticky", top: "0" });
    // Four of the virtual list's 44px touch rows (TOUCH_ROW_HEIGHT in AircraftListPanel).
    expect(px(phone(".aircraft-list-panel .al-list")["min-height"])).toBe(4 * 44);
    const toggle = phone(".aircraft-list-panel .al-collapse-btn");
    expect(px(toggle.width)).toBeGreaterThanOrEqual(44);
    expect(px(toggle.height)).toBeGreaterThanOrEqual(44);
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
    // The card is the 44px button and its 1px border, inset by its margin.
    const bottom = px(chip.margin) + px(phone(".aircraft-list-panel .al-collapse-btn").height) + 2 * 1;
    expect(px(phone(".live-map-area .leaflet-top.leaflet-left").top)).toBe(bottom);
  });
});

describe("the compact toolbar below desktop width", () => {
  it("keeps its one row on one line, the count giving way before the pill or a button", () => {
    expect(tablet(".toolbar-compact .toolbar-row-status")["flex-wrap"]).toBe("nowrap");
    expect(tablet(".toolbar-compact .connection-badge")["flex-shrink"]).toBe("0");
    expect(tablet(".toolbar-compact .toolbar-actions")).toMatchObject({ "flex-shrink": "0", "margin-left": "auto" });
    expect(tablet(".toolbar-compact .aircraft-count")).toMatchObject({
      "min-width": "0",
      overflow: "hidden",
      "text-overflow": "ellipsis",
    });
  });

  it("shows a phone its buttons as icons and the count as a plane and a number, the word kept for screen readers", () => {
    expect(phone(".toolbar-compact .toolbar-btn-text").display).toBe("none");
    expect(tablet(".aircraft-count-icon").display).toBe("none");
    expect(phone(".aircraft-count-icon").display).toBe("inline-block");
    const unit = phone(".aircraft-count-unit");
    expect(unit).toMatchObject({ position: "absolute", width: "1px", height: "1px", "clip-path": "inset(50%)" });
    expect(unit.display).toBeUndefined();
  });

  it("gives every switch in the Layers sheet a full-width row a finger can land on", () => {
    expect(tablet(".layers-sheet .layers-row")).toMatchObject({ width: "100%", "min-height": "44px" });
    expect(tablet(".layers-sheet .layers-filters .filter-clear")["min-height"]).toBe("44px");
    expect(touch(".toolbar-menu button")["min-height"]).toBe("44px");
  });

  it("sets the Layers sheet's fields at 16px wherever the sheet is drawn, so iOS does not zoom into them", () => {
    const fields = [
      ".app.map-surface .layers-sheet .filter-inputs input",
      ".app.map-surface .layers-sheet .filter-inputs select",
    ];
    for (const selector of fields) expect(tablet(selector)["font-size"]).toBe("16px");
    // Over the surface's 12px for its form controls, whichever stylesheet loads last.
    for (const control of ['.app.map-surface input[type="number"]', ".app.map-surface select"]) {
      expect(rule(surface.top, control)["font-size"]).toBe("12px");
      for (const selector of fields) expect(classWeight(selector)).toBeGreaterThan(classWeight(control));
    }
  });

  it("keeps the playback bar's controls clear of the home indicator", () => {
    expect(tablet(".playback-bar")["padding-bottom"]).toBe("calc(6px + env(safe-area-inset-bottom))");
  });

  it("keeps the toolbar and the playback bar clear of a notch at the screen's sides", () => {
    // The right edge at every width below desktop. The left only where each
    // bar starts at it: the toolbar below the drawer, where the sidebar no
    // longer stands there, and the playback bar on a phone, where the list
    // no longer stands beside it.
    expect(tablet(".live-map-toolbar")["padding-right"]).toBe("max(12px, env(safe-area-inset-right, 0px))");
    expect(tablet(".playback-bar")["padding-right"]).toBe("max(14px, env(safe-area-inset-right, 0px))");
    expect(rule(map.media.get(DRAWER), ".live-map-toolbar")["padding-left"]).toBe(
      "max(12px, env(safe-area-inset-left, 0px))",
    );
    expect(phone(".playback-bar")["padding-left"]).toBe("max(14px, env(safe-area-inset-left, 0px))");
    for (const [query, rules] of map.media) {
      for (const [selector, decls] of rules) {
        const left = Object.values(decls).filter((value) => value.includes("safe-area-inset-left"));
        if (selector.includes("live-map-toolbar") && query !== DRAWER) expect(left, `${query} ${selector}`).toEqual([]);
        if (selector.includes("playback-bar") && query !== PHONE) expect(left, `${query} ${selector}`).toEqual([]);
      }
    }
  });

  it("gives the playback slider a 44px band on touch, the browser's own track drawn across its middle", () => {
    expect(touch('.playback-bar input[type="range"]')).toMatchObject({
      height: "44px",
      "margin-top": "0",
      "margin-bottom": "0",
    });
    // Not restyled: a native range centres its track in the box it is given.
    expect(rule(map.top, '.playback-bar input[type="range"]').appearance).toBeUndefined();
    // The bar is then 57px, its 6px padding and 1px border round the band,
    // and the legend stands 16px above it, as it does on a desktop.
    const legend = rule(map.media.get(COARSE_TABLET), ".map-legend.with-playback:not(.map-legend-chip)");
    expect(legend.bottom).toBe("calc(73px + env(safe-area-inset-bottom, 0px))");
    expect(tablet(".map-legend.with-playback:not(.map-legend-chip)").bottom).toBe(
      "calc(46px + env(safe-area-inset-bottom, 0px))",
    );
  });

  it("styles none of it at desktop width", () => {
    const compactOnly = /toolbar-compact|toolbar-btn-text|toolbar-layers|aircraft-count-(icon|unit)|layers-|map-legend-chip/;
    expect([...map.top.keys()].filter((selector) => compactOnly.test(selector))).toEqual([]);
  });

  it("keeps the sheet's fields, the slider's band, the side insets and the legend's grid lines out of the desktop's rules", () => {
    const desktop = [map.top, ...[...map.media].filter(([query]) => !query.includes("max-width")).map(([, r]) => r)];
    for (const rules of desktop) {
      for (const [selector, decls] of rules) {
        expect(selector).not.toMatch(/layers-sheet/);
        for (const value of Object.values(decls)) expect(value, selector).not.toMatch(/safe-area-inset/);
        if (selector.includes("map-legend")) expect(decls["grid-area"], selector).toBeUndefined();
      }
    }
    expect(rule(map.top, '.playback-bar input[type="range"]').height).toBe("4px");
    expect(rule(map.top, ".live-map-toolbar").padding).toBe("0 12px");
    expect(rule(map.top, ".playback-bar").padding).toBe("6px 14px");
    expect(rule(map.top, ".map-legend.with-playback").bottom).toBe("46px");
  });
});

describe("the aircraft list below desktop width", () => {
  it("sets its search field and sort select at 16px on touch, so iOS does not zoom into them", () => {
    const fields = [
      ".app.map-surface .aircraft-list-panel .al-search input",
      ".app.map-surface .aircraft-list-panel .al-sort select",
    ];
    // A phone on its side is in the touch block's tablet half, past the
    // phone-only 16px of styles/phone.css. A desktop keeps the 12px.
    for (const selector of fields) {
      expect(touch(selector)["font-size"]).toBe("16px");
      expect(map.top.has(selector), selector).toBe(false);
    }
    // Over every 12px rule that reaches them, whichever stylesheet loads last.
    const twelve: [Map<string, Decls>, string][] = [
      [map.top, ".al-search input"],
      [surface.top, '.app.map-surface input[type="text"]'],
      [surface.top, ".app.map-surface select"],
    ];
    for (const [rules, control] of twelve) {
      expect(rule(rules, control)["font-size"]).toBe("12px");
      for (const selector of fields) expect(classWeight(selector)).toBeGreaterThan(classWeight(control));
    }
  });
});

describe("the list's and the legend's controls on a touch screen", () => {
  it("make the stats card's and the legend's headers and the owner's switch row 44px tall", () => {
    for (const selector of [".stats-panel-header", ".map-legend-header", ".owner-panel label"]) {
      expect(touch(selector)["min-height"], selector).toBe("44px");
      expect(rule(map.top, selector)["min-height"], selector).toBeUndefined();
    }
  });

  it("lay a 44px box over the rail's 22px collapse button on a touch tablet, inside the list that clips it", () => {
    const tabletTouch = map.media.get(TOUCH_TABLET);
    const edge = "min(0px, calc(50% - 22px))";
    expect(rule(tabletTouch, ".al-collapse-btn").position).toBe("relative");
    expect(rule(tabletTouch, ".al-collapse-btn::after")).toEqual({
      content: '""',
      position: "absolute",
      top: edge,
      right: edge,
      bottom: edge,
      left: edge,
    });
    // The 22px button sits 11px + 11px = 22px below the list's top edge, so
    // its box starts at that edge rather than above it, where it is cut off.
    expect(rule(map.top, ".al-collapse-btn")).toMatchObject({ width: "22px", height: "22px" });
    expect(rule(tabletTouch, ".al-header")).toMatchObject({ "padding-top": "11px", "padding-bottom": "11px" });
    // Collapsed, the rail is 44px across inside its 1px border, the button in its middle.
    expect(rule(map.top, ".aircraft-list-panel")["border-right"]).toBe("1px solid var(--border)");
    expect(rule(tabletTouch, ".aircraft-list-panel.collapsed").width).toBe("45px");
    expect(rule(tabletTouch, ".aircraft-list-panel.collapsed .al-header")).toMatchObject({
      "justify-content": "center",
      "padding-right": "0",
      "padding-left": "0",
    });
  });
});

describe("the legend on a phone", () => {
  it("is a chip in the corner of the map's top row, so it sits above the bar whatever the bar's height", () => {
    const legend = phone(".live-map-area > .map-legend");
    // All four lines: placed absolutely, an item's `auto` end line is the
    // grid's far edge, which would stretch it over the bar's row as well.
    expect(legend).toMatchObject({ "grid-area": "1 / 1 / 2 / 2", left: "10px", bottom: "10px" });
    expect(phone(".map-legend-chip .map-legend-header")["min-height"]).toBe("44px");
  });

  it("is drawn over Leaflet's controls and under the list and detail sheets", () => {
    const legend = z(phone(".live-map-area > .map-legend"));
    expect(legend).toBeGreaterThan(1000);
    expect(legend).toBeLessThan(z(phone(".live-map-body > .aircraft-list-panel")));
    expect(legend).toBeLessThan(z(phone(".live-map-body > .detail-panel")));
  });

  it("opens its key as a card above the chip", () => {
    expect(phone(".live-map-area > .map-legend").overflow).toBe("visible");
    expect(phone(".map-legend-chip .map-legend-body")).toMatchObject({
      position: "absolute",
      bottom: "calc(100% + 8px)",
      background: "var(--bg-card)",
      border: "1px solid var(--border)",
    });
  });

  it("stays up with the archive bar", () => {
    for (const rules of map.media.values()) {
      for (const [selector, decls] of rules) {
        if (selector.includes("map-legend")) expect(decls.display, selector).toBeUndefined();
      }
    }
  });
});
