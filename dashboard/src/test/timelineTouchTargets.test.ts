import { describe, it, expect } from "vitest";
import main from "../main.tsx?raw";

/* The vendored timeline's zoom buttons and the ends of its range take a
   fingertip's 44px on a touch screen, on both pages that draw it, and a
   desktop is drawn as before. jsdom applies no media query and lays nothing
   out, so these read the rules themselves. The sheet is read through a glob,
   so a sheet that is missing fails here rather than breaking the import. */

const SHEET = "../styles/timeline.touch.css";
const sheet =
  (import.meta.glob("../styles/timeline.touch.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>)[SHEET] ?? "";

const TOUCH = "(max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";
const ZOOM = ".edsc-timeline-tools__section:first-child >";
const ENDS = ".edsc-timeline-list__markers >";

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

const rules = parse(sheet);

/** What the touch query gives `selector` for `property`, the last one as the cascade would have it. */
function underTouch(selector: string, property: string): string | undefined {
  return rules
    .filter((r) => r.media === TOUCH && r.selectors.includes(selector))
    .map((r) => r.declarations.get(property))
    .filter((v) => v !== undefined)
    .pop();
}

const px = (selector: string, property: string) => Number.parseFloat(underTouch(selector, property) ?? "0");

/** Where an end's box and its line land when the line belongs at `line`, as the library places it. */
function end(which: "start" | "end", line: number) {
  const own = `${ENDS} .edsc-timeline-list__temporal-${which}`;
  const width = px(`${ENDS} .edsc-timeline-list__temporal-marker`, "width");
  const left = line + px(own, "margin-left");
  const drawn = left + px(own, "padding-left");
  return { left, right: left + width, width, drawn, lineWidth: width - px(own, "padding-left") - px(own, "padding-right") };
}

describe("the timeline's touch stylesheet", () => {
  it("loads right after the phone stylesheet", () => {
    expect(main).toContain('import "./App.css";\nimport "./styles/phone.css";\nimport "./styles/timeline.touch.css";\n');
  });

  it("keeps every rule inside the touch query, so a desktop is drawn by the library alone", () => {
    expect(rules.length).toBeGreaterThan(5);
    expect(rules.filter((r) => r.media !== TOUCH).map((r) => r.selectors.join(", "))).toEqual([]);
  });

  // The library's own rules name one class each and load after this sheet.
  it("names two classes or more in every selector, so the library's rules lose in either order", () => {
    const selectors = rules.flatMap((r) => r.selectors);
    expect(selectors.length).toBeGreaterThan(5);
    expect(selectors.filter((s) => (s.match(/\.[a-z]/g) ?? []).length < 2)).toEqual([]);
  });
});

describe("the zoom buttons under a finger", () => {
  it("stack two 44px rows, Increase over Decrease, in a column with no padding of its own", () => {
    const column = ".edsc-timeline-tools > .edsc-timeline-tools__section:first-child";
    expect(underTouch(column, "display")).toBe("grid");
    expect(underTouch(column, "grid-template-rows")).toBe("44px 44px");
    expect(underTouch(column, "padding")).toBe("0");
    expect(underTouch(`${ZOOM} .edsc-timeline-tools__action`, "width")).toBe("44px");
    expect(underTouch(`${ZOOM} .edsc-timeline-tools__action`, "height")).toBe("44px");
    expect(underTouch(`${ZOOM} .edsc-timeline-tools__action:first-child`, "grid-area")).toBe("1 / 1");
    expect(underTouch(`${ZOOM} .edsc-timeline-tools__action:last-child`, "grid-area")).toBe("2 / 1");
  });

  it("lay the unit's name over both, passing its taps to the button under it", () => {
    expect(underTouch(`${ZOOM} .edsc-timeline-tools__label--resolution`, "grid-area")).toBe("1 / 1 / 3");
    expect(underTouch(`${ZOOM} .edsc-timeline-tools__label--resolution`, "pointer-events")).toBe("none");
  });
});

describe("the ends of the range under a finger", () => {
  it.each([1, 2, 3, 200])("are 44px boxes that do not overlap when %ipx apart, each line on its own edge", (gap) => {
    const start = end("start", 100);
    const finish = end("end", 100 + gap);
    expect([start.width, finish.width]).toEqual([44, 44]);
    expect(start.right).toBeLessThanOrEqual(finish.left);
    expect([start.drawn, start.lineWidth, finish.drawn, finish.lineWidth]).toEqual([100, 1, 100 + gap, 1]);
  });

  it("take the strip's top 44px and paint the page's colour on the line alone, with no halo round the box", () => {
    const box = `${ENDS} .edsc-timeline-list__temporal-marker`;
    expect(underTouch(box, "height")).toBe("44px");
    expect(underTouch(box, "box-sizing")).toBe("border-box");
    expect(underTouch(box, "background-clip")).toBe("content-box");
    expect(underTouch(box, "box-shadow")).toBe("none");
  });

  it("carry the line on to the strip's foot, in the end's own colour, with the start's diamond on its line", () => {
    expect(underTouch(".edsc-timeline-list > .edsc-timeline-list__markers", "container-type")).toBe("size");
    const line = `${ENDS} .edsc-timeline-list__temporal-marker::before`;
    expect(underTouch(line, "height")).toBe("100cqh");
    expect(underTouch(line, "width")).toBe("1px");
    expect(underTouch(line, "background-color")).toBe("inherit");
    expect(underTouch(`${ENDS} .edsc-timeline-list__temporal-start::before`, "left")).toBe("43px");
    expect(underTouch(`${ENDS} .edsc-timeline-list__temporal-start::after`, "left")).toBe("43px");
    expect(underTouch(`${ENDS} .edsc-timeline-list__temporal-end::before`, "left")).toBe("0");
  });
});
