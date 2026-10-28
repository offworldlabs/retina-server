import { describe, it, expect } from "vitest";
import sheet from "../styles/phone.css?raw";

/* The phone stylesheet's promises: fields iOS will not zoom into, targets a
   finger can hit, and a page that holds still behind the open drawer, all
   inside a max-width block so a desktop never sees them. jsdom applies no
   media query, so these read the rules themselves. */

const DRAWER = "(max-width: 768px)";
const NARROW = "(max-width: 1023.98px)";
const TOUCH = "(max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";
const TOUCH_TABLET = "(min-width: 641px) and (max-width: 1023.98px) and (pointer: coarse)";

type Rule = { media: string; selectors: string[]; declarations: Map<string, string> };

/** Every rule, with the @media prelude around it ("" at the top level). */
function parse(css: string): Rule[] {
  const rules: Rule[] = [];
  const walk = (text: string, media: string) => {
    let at = 0;
    for (let open = text.indexOf("{"); open >= 0; open = text.indexOf("{", at)) {
      let close = open + 1;
      for (let depth = 1; depth > 0 && close < text.length; close++) {
        depth += text[close] === "{" ? 1 : text[close] === "}" ? -1 : 0;
      }
      const prelude = text.slice(at, open).trim().replace(/\s+/g, " ");
      const body = text.slice(open + 1, close - 1);
      if (prelude.startsWith("@media")) walk(body, prelude.slice("@media".length).trim());
      else {
        const declarations = new Map<string, string>();
        for (const line of body.split(";")) {
          const colon = line.indexOf(":");
          if (colon > 0) declarations.set(line.slice(0, colon).trim(), line.slice(colon + 1).trim());
        }
        rules.push({ media, selectors: prelude.split(",").map((s) => s.trim()), declarations });
      }
      at = close;
    }
  };
  // Prose is free to hold a brace.
  walk(css.replace(/\/\*[\s\S]*?\*\//g, ""), "");
  return rules;
}

const rules = parse(sheet);

/** What `selector` is given for `property` in the block whose query is `media`. */
function declared(media: string, selector: string, property: string): string | undefined {
  // The last one, as the cascade would have it.
  return rules
    .filter((r) => r.media === media && r.selectors.includes(selector))
    .map((r) => r.declarations.get(property))
    .filter((v) => v !== undefined)
    .pop();
}

describe("the phone stylesheet", () => {
  it("has rules, so a parse that found none cannot pass", () => {
    expect(rules.length).toBeGreaterThan(20);
  });

  it("keeps every rule inside a max-width block, so a desktop is drawn by App.css alone", () => {
    expect(rules.filter((r) => !/\(max-width:/.test(r.media)).map((r) => r.selectors.join(", "))).toEqual([]);
  });
});

describe("the pane's padding", () => {
  it.each([
    [NARROW, "right", "max(var(--content-pad), env(safe-area-inset-right))"],
    [NARROW, "bottom", "max(var(--content-pad), env(safe-area-inset-bottom))"],
    [DRAWER, "left", "max(var(--content-pad), env(safe-area-inset-left))"],
  ])("in %s publishes its %s padding and pads with it", (media, side, value) => {
    expect(declared(media, ".content", `--content-pad-${side}`)).toBe(value);
    expect(declared(media, ".content", `padding-${side}`)).toBe(`var(--content-pad-${side})`);
  });

  it.each([
    [NARROW, "right"],
    [NARROW, "bottom"],
    [DRAWER, "left"],
  ])("in %s publishes 0px on its %s for the flush pane, which has no padding", (media, side) => {
    expect(declared(media, ".content.flush", `--content-pad-${side}`)).toBe("0px");
  });
});

describe("form fields on a phone or a touch tablet", () => {
  // Over every page's own size, some of which load after this sheet. A touch
  // tablet is as wide as an iPhone on its side, which zooms in just the same.
  it.each([".input", ".input-sm", "select", "textarea"])("set %s at 16px, so iOS does not zoom in on focus", (selector) => {
    expect(declared(TOUCH, selector, "font-size")).toBe("16px !important");
  });

  it("set every input that takes typing at 16px, and leave the rest alone", () => {
    const typed = rules.filter((r) => r.media === TOUCH).flatMap((r) => r.selectors).find((s) => s.startsWith("input:not("));
    expect(typed).toBeDefined();
    expect(declared(TOUCH, typed!, "font-size")).toBe("16px !important");
    for (const type of ["checkbox", "radio", "range", "color", "file", "hidden"]) {
      expect(typed).toContain(`:not([type="${type}"])`);
    }
  });
});

describe("drop-down selects on a phone or a touch tablet", () => {
  const DROPDOWN = ['select:not([multiple]):not([size])', 'select:not([multiple])[size="1"]'];
  /** The value with its line breaks and indents folded to single spaces. */
  const value = (selector: string, property: string) => declared(TOUCH, selector, property)?.replace(/\s+/g, " ");

  // WebKit holds a menu button at its own height, under any min-height.
  it.each(DROPDOWN)("draw %s as a plain box, which takes the 44px floor", (selector) => {
    expect(value(selector, "-webkit-appearance")).toBe("none");
    expect(value(selector, "appearance")).toBe("none");
    expect(declared(TOUCH, "select", "min-height")).toBe("44px !important");
  });

  // The padding lengthens a select's minimum width, which a flex or grid item
  // keeps at its longest option and so runs past its row.
  it.each(DROPDOWN)("let %s shrink to its row when it is a flex or grid item", (selector) => {
    expect(value(selector, "min-width")).toBe("0");
  });

  it("leave a list box to the platform", () => {
    const rule = rules.find((r) => r.media === TOUCH && r.declarations.get("appearance") === "none");
    expect(rule?.selectors).toEqual(DROPDOWN);
  });

  // Each over the `background` and `padding` shorthands the pages give their
  // selects, some loading after this sheet.
  it.each(DROPDOWN)("draw an arrow on %s in the theme's secondary ink, clear of the text", (selector) => {
    expect(value(selector, "background-image")).toBe(
      "linear-gradient(45deg, transparent 50%, var(--text-secondary) 50%), " +
        "linear-gradient(135deg, var(--text-secondary) 50%, transparent 50%) !important",
    );
    expect(value(selector, "background-position")).toBe("right 17px center, right 12px center !important");
    expect(value(selector, "background-size")).toBe("5px 5px !important");
    expect(value(selector, "background-repeat")).toBe("no-repeat !important");
    // The arrow spans 12 to 22px from the right edge, inside this padding.
    expect(value(selector, "padding-right")).toBe("32px !important");
  });
});

describe("targets on a phone or a touch tablet", () => {
  it.each([
    ".btn:not(.de-chip)",
    ".btn-sm",
    ".tab",
    ".pager button",
    ".theme-switch button",
    ".header-menu",
    ".header-user",
    ".user-dropdown-item",
    ".nav-item",
    ".leaflet-container .leaflet-bar.leaflet-control a",
  ])("make %s at least 44px tall", (selector) => {
    expect(declared(TOUCH, selector, "min-height")).toMatch(/^44px\b/);
  });

  it("give every text field and select the same 44px floor over a page's own height", () => {
    const typed = rules.filter((r) => r.media === TOUCH).flatMap((r) => r.selectors).find((s) => s.startsWith("input:not("));
    expect(typed).toBeDefined();
    for (const type of ["checkbox", "radio", "range", "color", "file", "hidden"]) {
      expect(typed).toContain(`:not([type="${type}"])`);
    }
    for (const selector of [".input", ".input-sm", typed!, "select", "textarea"]) {
      expect(declared(TOUCH, selector, "min-height")).toBe("44px !important");
    }
  });

  // A glyph with no label is as narrow as the glyph.
  it.each([".theme-switch button", ".header-menu", ".header-user", ".leaflet-container .leaflet-bar.leaflet-control a"])(
    "make %s at least 44px wide",
    (selector) => {
      expect(declared(TOUCH, selector, "min-width")).toBe("44px");
    },
  );

  it.each([".header-signin", ".sidebar-toggle", "td a:not(.node-link)"])("lay a box at least 44px each way over %s", (selector) => {
    expect(declared(TOUCH, selector, "position")).toBe("relative");
    const hit = `${selector}::after`;
    expect(declared(TOUCH, hit, "content")).toBe('""');
    expect(declared(TOUCH, hit, "position")).toBe("absolute");
    for (const edge of ["top", "right", "bottom", "left"]) {
      expect(declared(TOUCH, hit, edge)).toBe("min(0px, calc(50% - 22px))");
    }
  });
});

describe("table rows on a touch tablet", () => {
  // A row about 40px tall let the next row's 44px link box lie over this
  // one's. The box is centred on the link, which a badge or a second line can
  // move a couple of px off its row's middle, so the floor leaves 2px each side.
  it("hold every row of a table with a link to at least 48px, so each link's box keeps to its row", () => {
    expect(declared(TOUCH_TABLET, "tbody:has(td a) > tr", "height")).toBe("48px");
    expect(declared(TOUCH, "td a:not(.node-link)::after", "top")).toBe("min(0px, calc(50% - 22px))");
  });

  // A phone's rows are cards and other blocks, where a height is fixed.
  it("leave a phone's rows to their own height", () => {
    expect(rules.filter((r) => r.selectors.includes("tbody:has(td a) > tr")).map((r) => r.media)).toEqual([TOUCH_TABLET]);
  });
});

describe("the open drawer", () => {
  it("holds the page behind it still", () => {
    expect(declared(DRAWER, ".dashboard.menu-open .content", "overflow")).toBe("hidden");
    expect(declared(DRAWER, ".sidebar-scrim", "touch-action")).toBe("none");
    expect(declared(DRAWER, ".sidebar", "overscroll-behavior")).toBe("contain");
  });

  it("gives each entry a row a finger can hit", () => {
    expect(declared(DRAWER, ".nav-item", "min-height")).toBe("44px");
  });
});
