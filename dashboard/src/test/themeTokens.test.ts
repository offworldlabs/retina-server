import { describe, it, expect } from "vitest";
import rawCss from "../App.css?raw";

/* The palette itself, and the invariants that hold it together, live with the
   stylesheet that declares it — packages/shared/src/tokens.test.ts. What is
   asserted here is that this console's own rules stay theme-safe: they inherit
   both palettes and must therefore name no colour of their own. */

// Comments go first and everything below reads the remainder: prose is free to
// contain a semicolon, a brace or a hex code, and every one of those confuses a
// parser this small.
const bare = rawCss.replace(/\/\*[\s\S]*?\*\//g, "");

describe("the dashboard stylesheet", () => {
  // Every one of these is invisible or wrong in the other theme.
  it("hardcodes no colour", () => {
    expect(bare.match(/#[0-9a-f]{3,8}\b|\brgba?\([^)]*\)/gi) ?? []).toEqual([]);
  });

  // `color: white` is as invisible on navy as `color: #fff` is, and the hex
  // sweep above does not see it. `transparent` and `currentColor` are fine.
  it("hardcodes no named colour either", () => {
    const named = /:\s*(white|black|red|green|blue|grey|gray|silver|navy|teal|orange)\b/gi;
    expect(bare.match(named) ?? []).toEqual([]);
  });

  // Reading a palette token is the point; redeclaring one here would fork the
  // shared palette silently. The colon is what separates the two — `var(--x)`
  // has none.
  it("declares no palette token of its own", () => {
    expect(bare).not.toContain("color-scheme");
    expect(bare).not.toMatch(/--(bg|text|border|accent|success|warning|error)-[\w-]*\s*:/);
  });

  // A parse that matched nothing would pass all three sweeps above.
  it("is not empty", () => {
    expect(bare.length).toBeGreaterThan(1000);
  });
});

/* A style object that sets a palette token repaints everything beneath it: a
   card that sets --accent to its own colour takes every focus ring, checkbox
   and primary button inside it along. A component with a colour of its own
   gives it a property of its own. */
describe("the components", () => {
  const all = import.meta.glob("../**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
  const sources = Object.fromEntries(Object.entries(all).filter(([path]) => !path.startsWith("../test/")));

  it("are found, so a glob that matched nothing cannot pass", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it("override no palette token inline", () => {
    const overrides = Object.entries(sources).flatMap(([path, source]) =>
      (source.match(/["']--(bg|text|border|accent|success|warning|error)(-[\w-]+)?["']\s*:/g) ?? []).map(
        (found) => `${path}: ${found}`,
      ),
    );
    expect(overrides).toEqual([]);
  });
});
