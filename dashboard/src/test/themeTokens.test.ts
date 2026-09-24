import { describe, it, expect } from "vitest";

/* The palette itself, and the invariants that hold it together, live with the
   stylesheet that declares it — packages/shared/src/tokens.test.ts. What is
   asserted here is that this console's own stylesheets stay theme-safe: their
   rules inherit both palettes and must therefore name no colour of their own. */

/* Every stylesheet under src, so one added later is swept without being listed. */
const sheets = import.meta.glob("../**/*.css", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

/* The one place a colour may be named: as the value a token is declared with,
   once per theme, so every rule that spends the token stays theme-safe. */
const TOKEN_DECLARATIONS: Record<string, RegExp> = {
  // The map's chrome: panel elevation, chips drawn onto tiles, the dialog
  // scrim, the Physics Layer's violet, and the map's deltas over the palette.
  "../pages/map/map-surface.css": /--[\w-]+\s*:[^;]*;/g,
  // The Data Explorer's scrim.
  "../pages/user/dataExplorer/dataExplorer.css": /--de-scrim\s*:[^;]*;/g,
  // The scrim behind the narrow screen's navigation drawer.
  "../App.css": /--nav-scrim\s*:[^;]*;/g,
};

/* The surface stylesheet exists to lay the map's deltas over the palette, so
   it is the one sheet allowed to redeclare a palette token. */
const MAP_SURFACE = "../pages/map/map-surface.css";

describe("the stylesheets", () => {
  // A glob that matched nothing would pass every sweep below.
  it("include every surface's", () => {
    expect(Object.keys(sheets)).toEqual(
      expect.arrayContaining([
        "../App.css",
        "../pages/map/LiveAircraftMap.css",
        "../pages/map/PhysicsSettings.css",
        ...Object.keys(TOKEN_DECLARATIONS),
      ]),
    );
  });
});

describe.each(Object.keys(sheets))("%s", (path) => {
  // Comments go first and everything below reads the remainder: prose is free
  // to contain a semicolon, a brace or a hex code, and every one of those
  // confuses a parser this small.
  const bare = sheets[path].replace(/\/\*[\s\S]*?\*\//g, "");
  const rules = TOKEN_DECLARATIONS[path] ? bare.replace(TOKEN_DECLARATIONS[path], "") : bare;

  // Every one of these is invisible or wrong in the other theme.
  it("hardcodes no colour", () => {
    expect(rules.match(/#[0-9a-f]{3,8}\b|\brgba?\([^)]*\)/gi) ?? []).toEqual([]);
  });

  // `color: white` is as invisible on navy as `color: #fff` is, and the hex
  // sweep above does not see it. `transparent` and `currentColor` are fine.
  it("hardcodes no named colour either", () => {
    const named = /:\s*(white|black|red|green|blue|grey|gray|silver|navy|teal|orange)\b/gi;
    expect(rules.match(named) ?? []).toEqual([]);
  });

  // Reading a palette token is the point; redeclaring one would fork the
  // shared palette silently. The colon is what separates the two — `var(--x)`
  // has none. The same holds for `color-scheme`, which the lookbehind keeps
  // apart from the `prefers-color-scheme` media feature.
  if (path !== MAP_SURFACE) {
    it("declares no palette token of its own", () => {
      expect(bare).not.toMatch(/(?<![\w-])color-scheme\s*:/);
      expect(bare).not.toMatch(/--(bg|text|border|accent|success|warning|error)(-[\w-]+)?\s*:/);
    });
  }

  // A parse that matched nothing would pass every sweep above.
  it("is not empty", () => {
    expect(rules.length).toBeGreaterThan(1000);
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
