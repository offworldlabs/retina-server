import { describe, it, expect } from "vitest";
import rawCss from "../App.css?raw";

const css = rawCss;

// Comments go first and everything below reads the remainder: prose is free to
// contain a semicolon, a brace or a hex code, and every one of those confuses a
// parser this small.
const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of the first rule whose selector matches. Good enough for a
 *  stylesheet of flat, hand-authored blocks, and it fails loudly rather than
 *  silently matching nothing. */
function block(selector: string): Record<string, string> {
  const at = bare.indexOf(selector);
  expect(at, `no rule for ${selector}`).toBeGreaterThan(-1);
  const open = bare.indexOf("{", at);
  const close = bare.indexOf("}", open);
  const declarations: Record<string, string> = {};
  for (const line of bare.slice(open + 1, close).split(";")) {
    const [prop, ...rest] = line.split(":");
    const value = rest.join(":").trim();
    const name = prop.trim();
    if (name && value) declarations[name] = value;
  }
  return declarations;
}

const light = block(":root {");
const systemDark = block(':root:not([data-theme="light"])');
const explicitDark = block(':root[data-theme="dark"]');

describe("the two dark blocks", () => {
  // CSS cannot share a declaration block across a media query boundary, so the
  // OS-preference copy and the explicit-choice copy are written twice. Nothing
  // in the stylesheet stops them drifting; this does.
  it("are identical", () => {
    expect(systemDark).toEqual(explicitDark);
  });

  it("are not empty, so a parse that matched nothing cannot pass", () => {
    expect(Object.keys(explicitDark).length).toBeGreaterThan(15);
  });
});

describe("the dark palette", () => {
  // A token declared in one theme and not the other resolves to nothing in the
  // theme that lacks it, which is a blank fill rather than a wrong colour.
  it("answers every colour the light palette declares", () => {
    const colours = (b: Record<string, string>) =>
      Object.keys(b).filter((k) => k.startsWith("--") && !/^--(sidebar|header|radius)/.test(k));
    expect(colours(explicitDark).sort()).toEqual(colours(light).sort());
  });

  it("is the map's, not a new one", () => {
    expect(explicitDark["--bg-primary"]).toBe("#0d1b2a");
    expect(explicitDark["--bg-card"]).toBe("#132240");
    expect(explicitDark["--accent"]).toBe("#38bdf8");
    expect(explicitDark["--text-primary"]).toBe("#e2e8f0");
  });

  it("tells the browser to theme its own furniture too", () => {
    expect(light["color-scheme"]).toBe("light");
    expect(explicitDark["color-scheme"]).toBe("dark");
  });
});

describe("the stylesheet", () => {
  // Every one of these is invisible or wrong in the other theme.
  it("hardcodes no colour outside the token blocks", () => {
    const lastToken = bare.indexOf(':root[data-theme="dark"]');
    const afterTokens = bare.slice(bare.indexOf("}", bare.indexOf("{", lastToken)));
    const hardcoded = afterTokens.match(/#[0-9a-f]{3,8}\b|\brgba?\([^)]*\)/gi) ?? [];
    expect(hardcoded).toEqual([]);
  });

  // `color: white` is as invisible on navy as `color: #fff` is, and the hex
  // sweep above does not see it. `transparent` and `currentColor` are fine.
  it("hardcodes no named colour either", () => {
    const named = /:\s*(white|black|red|green|blue|grey|gray|silver|navy|teal|orange)\b/gi;
    expect(bare.match(named) ?? []).toEqual([]);
  });
});
