import { describe, it, expect } from "vitest";
import nodeManagement from "../pages/admin/nodeManagement.css?raw";
import nodeDetail from "../pages/user/nodeDetail/nodeDetail.css?raw";
import nodeManagementSource from "../pages/admin/NodeManagementPage.tsx?raw";
import userDetailSource from "../pages/admin/UserDetailPage.tsx?raw";
import radarSource from "../pages/user/RadarPage.tsx?raw";
import scalarTables from "../utils/scalarTables.css?raw";
import phone from "../styles/phone.css?raw";

/* The small links and Scalar's narrow-layout controls reach a finger's 44px
   only under the touch query, so a desktop is drawn as it was. jsdom applies
   no media query, so these read the rules themselves. */

const TOUCH = "(max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";
const EXTENT = "min(0px, calc(50% - 22px))";

/** Every stylesheet under src, by its path from here. */
const sheets = import.meta.glob("../**/*.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

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
  walk(css.replace(/\/\*[\s\S]*?\*\//g, ""), "");
  return rules;
}

/** What the touch query gives `selector` for `property`, the last one as the cascade would have it. */
function underTouch(css: string, selector: string, property: string): string | undefined {
  return parse(css)
    .filter((r) => r.media === TOUCH && r.selectors.includes(selector))
    .map((r) => r.declarations.get(property))
    .filter((v) => v !== undefined)
    .pop();
}

/** Every declaration of the sheet sits in a block that no desktop matches. */
function onlyUnderMaxWidth(css: string): string[] {
  return parse(css)
    .filter((r) => !/\(max-width: [\d.]+px\)/.test(r.media))
    .map((r) => r.selectors.join(", "));
}

/** The 44px box laid over a control whose look stays as it is. */
function expectOverlay(css: string, control: string) {
  expect(underTouch(css, control, "position")).toBe("relative");
  expect(underTouch(css, `${control}::after`, "content")).toBe('""');
  expect(underTouch(css, `${control}::after`, "position")).toBe("absolute");
  for (const side of ["top", "right", "bottom", "left"]) {
    expect(underTouch(css, `${control}::after`, side)).toBe(EXTENT);
  }
}

/** That box, from the two lists styles/phone.css keeps for it: the rule that
 *  positions each control and the rule that lays the box over it. */
function expectSharedOverlay(control: string) {
  const touch = parse(phone).filter((r) => r.media === TOUCH);
  expect(touch.find((r) => r.selectors.includes(".header-signin"))?.selectors).toContain(control);
  expect(touch.find((r) => r.selectors.includes(".header-signin::after"))?.selectors).toContain(`${control}::after`);
  expectOverlay(phone, control);
}

/** An edge's offset in px on a box `height` px tall, in the two forms these
 *  rules write: a length, or min(0px, calc(N% - Mpx)). */
function offset(value: string | undefined, height: number): number {
  const scaled = /^min\(0px, calc\(([\d.]+)% - ([\d.]+)px\)\)$/.exec(value ?? "");
  if (scaled) return Math.min(0, (Number(scaled[1]) / 100) * height - Number(scaled[2]));
  const fixed = /^(-?[\d.]+)px$/.exec(value ?? "");
  if (fixed) return Number(fixed[1]);
  throw new Error(`no offset read from ${value}`);
}

describe("the 44px box centred on a control whose look stays as it is", () => {
  // One rule lays it, so its size is set in one place.
  it("is laid by styles/phone.css alone", () => {
    expect(Object.keys(sheets)).toContain("../styles/phone.css");
    const elsewhere = Object.entries(sheets)
      .filter(([file]) => file !== "../styles/phone.css")
      .flatMap(([file, css]) =>
        parse(css)
          .filter((r) => r.media === TOUCH && ["top", "right", "bottom", "left"].every((side) => r.declarations.get(side) === EXTENT))
          .flatMap((r) => r.selectors.filter((s) => s.endsWith("::after")).map((s) => `${file}: ${s}`)),
      );
    expect(elsewhere).toEqual([]);
  });

  // A page's sheet can load after styles/phone.css, so a rule of its own for
  // one of these boxes would reshape it.
  it("is given no other shape by a page's own sheet", () => {
    const shared = parse(phone).find((r) => r.media === TOUCH && r.selectors.includes(".header-signin::after"))!.selectors;
    const reshaped = Object.entries(sheets)
      .filter(([file]) => file !== "../styles/phone.css")
      .flatMap(([file, css]) =>
        parse(css).flatMap((r) => r.selectors.filter((s) => shared.includes(s)).map((s) => `${file}: ${s}`)),
      );
    expect(reshaped).toEqual([]);
  });
});

describe("the radar page's link to the node's page", () => {
  it("is given the shared 44px box under the touch query", () => {
    expectSharedOverlay(".radar-detail-link");
  });

  it("carries the class the rule reads", () => {
    expect(radarSource).toContain('className="radar-detail-link"');
  });
});

describe("the node name's link in the heading on a touch screen", () => {
  it("is positioned, so the note below the heading cannot cover the foot of its box", () => {
    expect(underTouch(nodeDetail, ".node-detail-title .retnode-link", "position")).toBe("relative");
  });
});

describe("the link in a card of the Nodes page", () => {
  const link = ".managed-node-card .node-name .retnode-link";
  const name = ".node-card.managed-node-card .node-name";

  it("is given the shared 44px box under the touch query, and the page's sheet lays none of its own", () => {
    expectSharedOverlay(link);
    expect(parse(nodeManagement).map((r) => r.selectors)).toEqual([[name]]);
  });

  // The link is 21px tall, 0.5px inside the name's 22px line, and the name is
  // the first thing in its card, under the card's 16px padding. Below the
  // name lie the card's details, where a tap opens the node's page.
  it("has room for its box between the card's edge and the card's details", () => {
    const margin = underTouch(nodeManagement, name, "margin-bottom");
    expect(margin).toBe("12px");
    expect(underTouch(phone, `${link}::after`, "bottom")).toBe(EXTENT);
    const height = 21;
    const inset = (22 - height) / 2;
    const edge = (side: string) => offset(underTouch(phone, `${link}::after`, side), height);
    expect(height - edge("top") - edge("bottom")).toBe(44);
    expect(-edge("top") - inset).toBeLessThanOrEqual(16);
    expect(offset(margin, height) - (-edge("bottom") - inset)).toBeGreaterThanOrEqual(0.5);
  });

  it("widens the name's margin over the one App.css gives every card's name, whichever sheet loads last", () => {
    const theirs = parse(sheets["../App.css"])
      .filter((r) => r.declarations.has("margin-bottom"))
      .flatMap((r) => r.selectors.filter((s) => s.endsWith(".node-name")));
    expect(theirs).toEqual([".node-card .node-name"]);
    const classes = (selector: string) => selector.match(/\.[\w-]+/g)?.length ?? 0;
    expect(classes(name)).toBeGreaterThan(classes(theirs[0]));
  });

  it("is laid under the touch query only", () => {
    expect(onlyUnderMaxWidth(nodeManagement)).toEqual([]);
  });

  it("is read from the class the page's cards carry, and the sheet is loaded", () => {
    expect(nodeManagementSource).toContain('className="node-card managed-node-card"');
    expect(nodeManagementSource).toContain('import "./nodeManagement.css";');
  });
});

describe("the way back on an account's page", () => {
  it("is given the shared 44px box under the touch query", () => {
    expectSharedOverlay(".detail-back");
  });

  it("carries the class the rule reads", () => {
    expect(userDetailSource).toContain('className="detail-back"');
  });
});

describe("Scalar's controls on a touch screen", () => {
  it("lay the shared 44px box over the menu button", () => {
    expectSharedOverlay(".scalar-app .t-doc__header > header > .scalar-icon-button");
  });

  // A box over the picker lay under the auth section's panel, which is
  // positioned and later in the page, so the picker grows to 44px itself.
  it("grow the auth-type picker to 44px and lay no box over it", () => {
    const picker = '.scalar-app [class~="group/combobox-button"]';
    expect(underTouch(scalarTables, picker, "min-height")).toBe("44px");
    expect(underTouch(scalarTables, picker, "position")).toBeUndefined();
    expect(underTouch(scalarTables, `${picker}::after`, "content")).toBeUndefined();
  });

  it("grow the client-library tabs to 44px, and the button for the full list to 44px wide", () => {
    expect(underTouch(scalarTables, ".scalar-app .client-libraries-list .client-libraries", "min-height")).toBe("44px");
    expect(underTouch(scalarTables, ".scalar-app .client-libraries-list .client-libraries__select", "min-width")).toBe("44px");
  });

  it("changes nothing outside a max-width block", () => {
    expect(onlyUnderMaxWidth(scalarTables)).toEqual([]);
  });
});
