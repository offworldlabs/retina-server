import { describe, it, expect } from "vitest";
import appCss from "../App.css?raw";
/* Read by path rather than through the package's exports map, which a `?raw`
   query does not survive. */
import uiCss from "../../../packages/shared/css/ui.css?raw";
import { declarations } from "./paletteTokens";

/* The console pages draw with classes from ui.css and App.css. An inline style
   that spells one out again outranks the class, so the two drift apart and the
   page stops following the stylesheet. These sweeps read the page sources for
   the shapes that have a class to use instead. The map and the Data Explorer
   keep their own idioms and are not swept. */

const sources = import.meta.glob("../pages/{admin,user}/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const pages = Object.entries(sources)
  .filter(([path]) => !path.endsWith("DataExplorerPage.tsx"))
  .map(([path, source]) => [path, source.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, "")] as const);

const ui = uiCss.replace(/\/\*[\s\S]*?\*\//g, "");
const app = appCss.replace(/\/\*[\s\S]*?\*\//g, "");

function sweep(pattern: RegExp) {
  return pages.flatMap(([path, source]) => (source.match(pattern) ?? []).map((m) => `${path}: ${m}`));
}

describe("the console page sweep", () => {
  it("finds pages to check, so a glob that matched nothing cannot pass", () => {
    expect(pages.length).toBeGreaterThan(15);
  });
});

describe("text", () => {
  it("defines the three text classes in the shared vocabulary", () => {
    expect(declarations(ui, ".muted {").color).toBe("var(--text-muted)");
    expect(declarations(ui, ".card-note {")).toEqual({ "font-size": "12px", color: "var(--text-muted)" });
    expect(declarations(ui, ".mono {")["font-size"]).toBe("12px");
  });

  it("sets ids in .mono", () => {
    expect(sweep(/fontFamily: "monospace"/g)).toEqual([]);
  });

  it("mutes text with .muted, .card-note or .reading-label", () => {
    // Whatever else the style object holds: a muted colour set inline is one
    // of the three, or it is a colour the page has picked for itself.
    expect(sweep(/style=\{\{[^}]*color: "var\(--text-muted\)"[^}]*\}\}/g)).toEqual([]);
  });
});

describe("stacking and layout", () => {
  it("gives a card on the page the same gap below it as every other block", () => {
    const gap = declarations(app, ".content > .card {")["margin-bottom"];
    expect(gap).toBe("24px");
    expect(declarations(app, ".grid-2 {")["margin-bottom"]).toBe(gap);
  });

  it("leaves card spacing to the stylesheet", () => {
    expect(sweep(/className="(?:card|grid-2|stats-grid)"[^>]*style=\{\{[^}]*margin/g)).toEqual([]);
  });

  it("pads a card's contents with .card-body", () => {
    expect(sweep(/padding: "0 20px 16px"/g)).toEqual([]);
  });

  it("lays two cards side by side with .grid-2", () => {
    expect(sweep(/gridTemplateColumns: "1fr 1fr"/g)).toEqual([]);
  });

  it("spaces a badge by the group it sits in, not a margin of its own", () => {
    expect(sweep(/className="badge[^"]*" style=\{\{ margin/g)).toEqual([]);
  });

  it("styles no heading inline", () => {
    expect(sweep(/<h[1-6] style=\{\{[^}]*fontSize/g)).toEqual([]);
  });
});
