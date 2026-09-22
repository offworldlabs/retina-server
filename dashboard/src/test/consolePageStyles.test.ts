import { describe, it, expect } from "vitest";
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

  it("mutes text with .muted or .card-note", () => {
    expect(sweep(/style=\{\{ (?:fontSize: \d+, )?color: "var\(--text-muted\)"(?:, fontSize: \d+)? \}\}/g)).toEqual([]);
  });
});
