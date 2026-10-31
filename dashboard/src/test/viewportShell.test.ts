import { describe, it, expect } from "vitest";
import indexHtml from "../../index.html?raw";
import appCss from "../App.css?raw";
import tokensCss from "../../../packages/shared/css/tokens.css?raw";
import main from "../main.tsx?raw";

/* A phone gives the page its whole screen, notch and toolbars included, and
   the desktop above 1023px is drawn exactly as before. These read the page,
   the stylesheet and the entry point for the pieces that make both true. */

const head = new DOMParser().parseFromString(indexHtml, "text/html").head;

/** Every `height` the rules naming `selector` declare, each with the @media
 *  around it, or "" at the top level. */
function heights(selector: string): [media: string, value: string][] {
  const found: [string, string][] = [];
  const walk = (css: string, media: string) => {
    let at = 0;
    for (let open = css.indexOf("{"); open >= 0; open = css.indexOf("{", at)) {
      let close = open + 1;
      for (let depth = 1; depth > 0; close++) depth += css[close] === "{" ? 1 : css[close] === "}" ? -1 : 0;
      const prelude = css.slice(at, open).trim();
      const body = css.slice(open + 1, close - 1);
      if (prelude.startsWith("@media")) walk(body, prelude);
      else if (prelude.split(",").map((s) => s.trim()).includes(selector)) {
        for (const line of body.split(";")) {
          const value = /^\s*height\s*:\s*(.+?)\s*$/.exec(line)?.[1];
          if (value) found.push([media, value]);
        }
      }
      at = close;
    }
  };
  walk(appCss.replace(/\/\*[\s\S]*?\*\//g, ""), "");
  return found;
}

describe("the page on a phone", () => {
  // Without viewport-fit the browser letterboxes the page inside the notch.
  // Pinch zoom stays: nothing caps the scale or turns it off.
  it("reaches the screen's edges and can still be zoomed", () => {
    const viewport = head.querySelector('meta[name="viewport"]')?.getAttribute("content");
    expect(viewport).toBe("width=device-width, initial-scale=1.0, viewport-fit=cover");
  });

  it("colours the browser bar like the header in either OS scheme", () => {
    const tags = [...head.querySelectorAll('meta[name="theme-color"]')].map((m) => [
      m.getAttribute("media"),
      m.getAttribute("content"),
    ]);
    expect(tags).toEqual([
      ["(prefers-color-scheme: light)", "#ffffff"],
      ["(prefers-color-scheme: dark)", "#132240"],
    ]);
  });

  // The tags cannot read a token, so they repeat its two values; this is what
  // notices when the palette moves and the tags do not.
  it("keeps those colours the header's own", () => {
    expect(appCss).toMatch(/\n\.header \{[^}]*background: var\(--bg-secondary\);/);
    expect(tokensCss).toMatch(/\n:root,\n\.map-surface\[data-theme="light"\] \{[^}]*--bg-secondary: #ffffff;/);
    expect(tokensCss).toMatch(/\n:root\[data-theme="dark"\] \{[^}]*--bg-secondary: #132240;/);
  });
});

describe("the console's shell", () => {
  // `vh` is the screen with the toolbars put away, so the shell's bottom sat
  // under them; `dvh` follows them. Up to tablets only, with `vh` as the
  // fallback, so a desktop keeps the height it had.
  it.each([".dashboard", ".loading-screen"])("%s takes the visible height below desktop widths", (selector) => {
    expect(heights(selector)).toEqual([
      ["", "100vh"],
      ["@media (max-width: 1023.98px)", "100dvh"],
    ]);
  });

  it("loads the phone stylesheet after App.css, so its rules win", () => {
    expect(main).toContain('import "./App.css";\nimport "./styles/phone.css";\n');
  });
});
