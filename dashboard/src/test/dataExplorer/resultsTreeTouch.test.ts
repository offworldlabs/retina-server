import { describe, expect, it } from "vitest";

import explorerCss from "../../pages/user/dataExplorer/dataExplorer.css?raw";

/* A touch tablet draws the results tree as the desktop does, rows under
   column heads, and every control on it takes a fingertip's 44px each way. A
   phone draws cards, whose group heads keep a caret beside their checkbox's
   44px label. jsdom applies no media query and lays nothing out, so these
   read the rules themselves: those under the touch query, which no desktop
   matches. */

const TOUCH = "(max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";

type Rule = { media: string; selectors: string[]; declarations: Map<string, string> };

/** Every rule, with the innermost @media prelude around it ("" at the top level). */
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

const RULES = parse(explorerCss);

/** What the touch query gives `selector` for `property`, the last one as the cascade would have it. */
function underTouch(selector: string, property: string): string | undefined {
  return RULES.filter((r) => r.media === TOUCH && r.selectors.includes(selector))
    .map((r) => r.declarations.get(property))
    .filter((v) => v !== undefined)
    .pop();
}

const BARE_CHECKBOXES = [
  '.de-day-head > input[type="checkbox"]',
  '.de-node-head > input[type="checkbox"]',
  '.de-file > input[type="checkbox"]',
];

describe("the results tree under a finger", () => {
  it("gives a column's sort button 44px each way and keeps its label on its line", () => {
    const sort = (property: string) => underTouch(".de-colhead button", property);
    expect(sort("min-width")).toBe("44px");
    expect(sort("min-height")).toBe("44px");
    // The label's line is 18px, 12px type at 1.5: 13px out on either side
    // keeps it at 18px.
    expect(sort("margin-block")).toBe("-13px");
    expect(sort("text-align")).toBe("left");
    // Above the Actions line under it, which the reach crosses the gap into.
    expect(sort("position")).toBe("relative");
  });

  // No label wraps a head's or a row's checkbox on a tablet, so its own box
  // is all a tap can land on.
  it("makes each bare checkbox a 44px box of its own, the glyph drawn 18px across inside it", () => {
    for (const box of BARE_CHECKBOXES) {
      expect(underTouch(box, "appearance"), box).toBe("none");
      expect(underTouch(box, "width"), box).toBe("44px");
      expect(underTouch(box, "height"), box).toBe("44px");
      expect(underTouch(box, "border"), box).toBe("13px solid transparent");
    }
    // A file's checkbox has a column as wide as it, and a row that starts
    // 13px above the name's line, as far as the box reaches past it.
    expect(underTouch(".de-tree", "--de-cols")).toMatch(/^44px /);
    expect(underTouch('.de-file > input[type="checkbox"]', "margin-block")).toBe("-13px");
    expect(underTouch(".de-file", "padding-top")).toBe("13px");
  });

  it("makes a group's head a fingertip tall, and its toggle as tall as the head", () => {
    for (const head of [".de-day-head", ".de-node-head"]) {
      expect(underTouch(head, "min-height"), head).toBe("44px");
      expect(underTouch(head, "padding-top"), head).toBe("0");
      expect(underTouch(head, "padding-bottom"), head).toBe("0");
    }
    expect(underTouch(".de-toggle", "align-self")).toBe("stretch");
  });

  // A collapsed group turns its caret, and the turned caret is drawn over the
  // checkbox beside it and wider than it was, into the checkbox's target.
  it("lets taps through a group's caret to what lies under it", () => {
    expect(underTouch(".de-caret", "pointer-events")).toBe("none");
  });
});
