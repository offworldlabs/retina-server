import { describe, expect, it } from "vitest";
import sheet from "../../pages/user/dataExplorer/basket.phone.css?raw";

/* The basket and the preview below desktop width. jsdom applies no media
   query, so these read the rules themselves: every one below 1024px, so a
   desktop is drawn as it always was, and the edges that meet the screen's
   kept clear of a notch and the home indicator. */

const TABLET = "(max-width: 1023.98px)";
const PHONE = "(max-width: 640px)";

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
          if (colon > 0) declarations.set(line.slice(0, colon).trim(), line.slice(colon + 1).trim().replace(/\s+/g, " "));
        }
        rules.push({ media, selectors: prelude.split(",").map((s) => s.trim()), declarations });
      }
      at = close;
    }
  };
  walk(css.replace(/\/\*[\s\S]*?\*\//g, ""), "");
  return rules;
}

const rules = parse(sheet);

/** What `selector` is given for `property` in the block whose query is `media`. */
function declared(media: string, selector: string, property: string): string | undefined {
  return rules
    .filter((r) => r.media === media && r.selectors.includes(selector))
    .map((r) => r.declarations.get(property))
    .filter((v) => v !== undefined)
    .pop();
}

describe("the basket and preview stylesheet", () => {
  it("keeps every rule inside a max-width block below 1024px", () => {
    expect(rules.length).toBeGreaterThan(0);
    const above = rules.filter((r) => {
      const width = /^\(max-width: ([\d.]+)px\)$/.exec(r.media);
      return !width || Number(width[1]) >= 1024;
    });
    expect(above.map((r) => `${r.media} ${r.selectors.join(", ")}`)).toEqual([]);
  });
});

describe("the basket below desktop width", () => {
  it.each([
    ["bottom", "calc(-1 * var(--content-pad-bottom, var(--content-pad)))"],
    ["margin-right", "calc(-1 * var(--content-pad-right, var(--content-pad)))"],
    ["margin-bottom", "calc(-1 * var(--content-pad-bottom, var(--content-pad)))"],
    ["margin-left", "calc(-1 * var(--content-pad-left, var(--content-pad)))"],
  ])("bleeds to the pane's edge by the padding it publishes: %s", (property, value) => {
    expect(declared(TABLET, ".content .de-basket", property)).toBe(value);
  });

  it("keeps the bar's content clear of the side and bottom insets", () => {
    expect(declared(TABLET, ".de-basket > .de-basket-bar", "padding-right")).toBe(
      "var(--content-pad-right, var(--content-pad))",
    );
    expect(declared(TABLET, ".de-basket > .de-basket-bar", "padding-left")).toBe(
      "var(--content-pad-left, var(--content-pad))",
    );
    expect(declared(TABLET, ".de-basket > .de-basket-bar:last-child", "padding-bottom")).toBe(
      "max(11px, env(safe-area-inset-bottom))",
    );
    expect(declared(TABLET, ".de-basket > .de-basket-body", "padding-bottom")).toBe(
      "max(16px, env(safe-area-inset-bottom))",
    );
    expect(declared(PHONE, ".de-basket.de-basket-compact > .de-basket-bar", "padding-bottom")).toBe(
      "max(8px, env(safe-area-inset-bottom))",
    );
  });
});

describe("the preview below desktop width", () => {
  it("keeps the drawer's head, body and foot clear of the right and bottom insets", () => {
    for (const part of ["head", "body", "foot"]) {
      expect(declared(TABLET, `.de-drawer > .de-drawer-${part}`, "padding-right")).toBe(
        "max(18px, env(safe-area-inset-right))",
      );
    }
    expect(declared(TABLET, ".de-drawer > .de-drawer-foot", "padding-bottom")).toBe(
      "max(12px, env(safe-area-inset-bottom))",
    );
  });

  it("stacks each label over its value in the phone's sheet, and scrolls the table in its frame", () => {
    expect(declared(PHONE, ".de-preview-sheet .de-kv", "grid-template-columns")).toBe("minmax(0, 1fr)");
    expect(declared(PHONE, ".de-preview-sheet .de-kv dd", "word-break")).toBe("normal");
    expect(declared(PHONE, ".de-preview-sheet .de-prev-wrap", "overflow-x")).toBe("auto");
    expect(declared(PHONE, ".de-preview-sheet", "--de-mono")).toMatch(/monospace$/);
  });
});
