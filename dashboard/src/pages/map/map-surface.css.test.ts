import { describe, expect, it } from "vitest";
import css from "./map-surface.css?raw";
import touchCss from "../../styles/phone.css?raw";

const PHONE = "(max-width: 640px)";
const TOUCH = "(max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";

/** A stylesheet without its comments, runs of whitespace made one space. */
const bare = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ");
const flat = bare(css);
const phone = bare(touchCss);

/** Where the brace opening at `open` closes. */
function closing(text: string, open: number): number {
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}" && --depth === 0) return i;
  }
  throw new Error(`unbalanced braces after ${open}`);
}

/** Each selector in the sheet's first @media block with exactly this query,
 *  with its declarations by property. */
function media(query: string, sheet = flat): Map<string, Record<string, string>> {
  const at = sheet.indexOf(`@media ${query} {`);
  expect(at, `@media ${query} is missing`).toBeGreaterThanOrEqual(0);
  const open = sheet.indexOf("{", at);
  const rules = new Map<string, Record<string, string>>();
  for (const [, selectors, body] of sheet.slice(open + 1, closing(sheet, open)).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const decls = Object.fromEntries(
      body.split(";").filter((d) => d.includes(":")).map((d) => [d.slice(0, d.indexOf(":")).trim(), d.slice(d.indexOf(":") + 1).trim()]),
    );
    for (const selector of selectors.split(",")) rules.set(selector.trim(), { ...rules.get(selector.trim()), ...decls });
  }
  return rules;
}

/** The selectors of the rule in `sheet` whose list holds `member`. */
function listWith(sheet: string, member: string): string[] {
  for (const [, selectors] of sheet.matchAll(/([^{}]+)\{[^{}]*\}/g)) {
    const list = selectors.split(",").map((s) => s.trim());
    if (list.includes(member)) return list;
  }
  return [];
}

/** The stylesheet as a desktop draws it: every @media block dropped. */
function desktop(): string {
  let text = flat;
  for (let at = text.indexOf("@media"); at >= 0; at = text.indexOf("@media")) {
    text = text.slice(0, at) + text.slice(closing(text, text.indexOf("{", at)) + 1);
  }
  return text;
}

const SCROLLS = { "touch-action": "pan-y pinch-zoom", "overscroll-behavior": "contain" };
const HOLDS_STILL = { "touch-action": "pinch-zoom" };

describe("a drag on a panel over the map", () => {
  it("scrolls the phone's list and detail sheets and stops at their ends", () => {
    const phone = media(PHONE);
    for (const scroller of [
      ".app.map-surface .live-map-body > .aircraft-list-panel",
      ".app.map-surface .aircraft-list-panel .al-list",
      ".app.map-surface .detail-panel-body",
    ]) {
      expect(phone.get(scroller), scroller).toMatchObject(SCROLLS);
    }
  });

  it("pans nothing from the collapsed list's chip or the detail sheet's header", () => {
    const phone = media(PHONE);
    expect(phone.get(".app.map-surface .live-map-body > .aircraft-list-panel.collapsed")).toMatchObject(HOLDS_STILL);
    expect(phone.get(".app.map-surface .live-map-body > .detail-panel .detail-panel-header")).toMatchObject(HOLDS_STILL);
  });

  it("scrolls the shortcut list and pans nothing from the legend or the dialog's scrim on a touch screen", () => {
    const touch = media(TOUCH);
    expect(touch.get(".app.map-surface .shortcut-dialog")).toMatchObject(SCROLLS);
    expect(touch.get(".app.map-surface .map-legend")).toMatchObject(HOLDS_STILL);
    expect(touch.get(".app.map-surface .shortcut-backdrop")).toMatchObject(HOLDS_STILL);
  });
});

/* styles/phone.css lays the 44px box over a control whose look stays as it
   is from one list; Leaflet places both of these absolutely, so their boxes
   hang from them and the sheet gives them no position of its own. */
const SHARED_BOX = {
  content: '""',
  position: "absolute",
  top: "min(0px, calc(50% - 22px))",
  right: "min(0px, calc(50% - 22px))",
  bottom: "min(0px, calc(50% - 22px))",
  left: "min(0px, calc(50% - 22px))",
};

describe("Leaflet's popup close button on a touch screen", () => {
  it("takes the shared 44px box around its 24px cross, hung from the button where Leaflet places it", () => {
    const close = ".app.map-surface .leaflet-container a.leaflet-popup-close-button";
    expect(listWith(phone, ".header-signin::after")).toContain(`${close}::after`);
    expect(media(TOUCH, phone).get(`${close}::after`)).toEqual(SHARED_BOX);
    expect(media(TOUCH, phone).get(close)).toBeUndefined();
  });
});

describe("a node's marker on a touch screen", () => {
  it("takes the shared 44px box around its 22px glyph, hung from the marker where Leaflet places it", () => {
    // Each edge is min(0, 11px - 22px) on the 22px icon: 11px out on every side.
    const marker = ".app.map-surface .leaflet-container .node-marker";
    expect(listWith(phone, ".header-signin::after")).toContain(`${marker}::after`);
    expect(media(TOUCH, phone).get(`${marker}::after`)).toEqual(SHARED_BOX);
    expect(media(TOUCH, phone).get(marker)).toBeUndefined();
  });
});

describe("the map surface on a desktop", () => {
  it("leaves gestures and Leaflet's controls as they were", () => {
    expect(desktop()).not.toMatch(/touch-action|overscroll-behavior|leaflet-/);
  });
});
