import { describe, it, expect } from "vitest";
import rawTokens from "../css/tokens.css?raw";
import rawUi from "../css/ui.css?raw";

// Comments go first and everything below reads the remainder: prose is free to
// contain a semicolon, a brace or a hex code, and every one of those confuses a
// parser this small.
const bare = rawTokens.replace(/\/\*[\s\S]*?\*\//g, "");

interface Rule {
  selector: string;
  declarations: Record<string, string>;
}

/** The first rule whose selector list mentions `anchor`, selector included —
 *  the palettes are shared by giving one block several selectors, so which
 *  selectors a block carries is itself an invariant worth asserting. Good
 *  enough for a stylesheet of flat, hand-authored blocks, and it fails loudly
 *  rather than silently matching nothing. */
function ruleAt(anchor: string): Rule {
  const at = bare.indexOf(anchor);
  expect(at, `no rule mentioning ${anchor}`).toBeGreaterThan(-1);
  const open = bare.indexOf("{", at);
  const close = bare.indexOf("}", open);
  const start = Math.max(bare.lastIndexOf("}", at), bare.lastIndexOf("{", at)) + 1;
  const declarations: Record<string, string> = {};
  for (const line of bare.slice(open + 1, close).split(";")) {
    const [prop, ...rest] = line.split(":");
    const value = rest.join(":").trim();
    const name = prop.trim();
    if (name && value) declarations[name] = value;
  }
  return { selector: bare.slice(start, open).trim(), declarations };
}

const light = ruleAt('.map-surface[data-theme="light"]');
const systemDark = ruleAt(':root:not([data-theme="light"])');
const explicitDark = ruleAt(':root[data-theme="dark"]');

describe("the two dark blocks", () => {
  // CSS cannot share a declaration block across a media query boundary, so the
  // OS-preference copy and the explicit-choice copy are written twice. Nothing
  // in the stylesheet stops them drifting; this does.
  it("are identical", () => {
    expect(systemDark.declarations).toEqual(explicitDark.declarations);
  });

  it("are not empty, so a parse that matched nothing cannot pass", () => {
    expect(Object.keys(explicitDark.declarations).length).toBeGreaterThan(15);
  });
});

describe("the dark palette", () => {
  // A token declared in one theme and not the other resolves to nothing in the
  // theme that lacks it, which is a blank fill rather than a wrong colour.
  it("answers every colour the light palette declares", () => {
    const colours = (r: Rule) =>
      Object.keys(r.declarations).filter((k) => k.startsWith("--") && !/^--(header|radius)/.test(k));
    expect(colours(explicitDark).sort()).toEqual(colours(light).sort());
  });

  it("is the map's, not a new one", () => {
    expect(explicitDark.declarations["--bg-primary"]).toBe("#0d1b2a");
    expect(explicitDark.declarations["--bg-card"]).toBe("#132240");
    expect(explicitDark.declarations["--accent"]).toBe("#38bdf8");
    expect(explicitDark.declarations["--text-primary"]).toBe("#e2e8f0");
  });

  it("tells the browser to theme its own furniture too", () => {
    expect(light.declarations["color-scheme"]).toBe("light");
    expect(explicitDark.declarations["color-scheme"]).toBe("dark");
  });
});

describe("the surfaces the palettes reach", () => {
  // The consoles default to light and buy dark with the attribute; the map
  // inverts that. Swap either selector onto the other block and a surface
  // flashes the wrong theme on first paint, which no rendering test would
  // catch because both themes render perfectly well.
  it("give the consoles light by default", () => {
    expect(light.selector).toMatch(/(^|,)\s*:root\s*(,|$)/);
  });

  it("give the map dark by default, and only the map", () => {
    expect(explicitDark.selector).toContain('.map-surface:not([data-theme="light"])');
    expect(light.selector).toContain('.map-surface[data-theme="light"]');
    expect(systemDark.selector).not.toContain("map-surface");
  });

  // Without this the OS preference would be pinned at load instead of being
  // answered live by the media query.
  it("leave the OS preference to a surface carrying no attribute", () => {
    expect(systemDark.selector).toBe(':root:not([data-theme="light"])');
  });
});

describe("the shared component vocabulary", () => {
  const ui = rawUi.replace(/\/\*[\s\S]*?\*\//g, "");

  // Every one of these is invisible or wrong in the other theme.
  it("hardcodes no colour", () => {
    expect(ui.match(/#[0-9a-f]{3,8}\b|\brgba?\([^)]*\)/gi) ?? []).toEqual([]);
  });

  // `color: white` is as invisible on navy as `color: #fff` is, and the hex
  // sweep above does not see it. `transparent` and `currentColor` are fine.
  it("hardcodes no named colour either", () => {
    const named = /:\s*(white|black|red|green|blue|grey|gray|silver|navy|teal|orange)\b/gi;
    expect(ui.match(named) ?? []).toEqual([]);
  });

  // The surfaces layer their own deltas over these by writing a more specific
  // rule; a scoped selector here would make that impossible to outrank
  // predictably.
  it("scopes nothing to a surface", () => {
    expect(ui).not.toContain(".map-surface");
  });

  // One pressed state for every toggle, keyed to the ARIA attribute, so a
  // surface has no reason to invent an `.active` or `.on` of its own.
  it("draws a pressed toggle from its ARIA state", () => {
    expect(ui).toMatch(/\.btn\[aria-pressed="true"\][^{]*\{[^}]*background:\s*var\(--accent-light\)/);
  });
});
