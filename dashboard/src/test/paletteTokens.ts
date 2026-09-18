import { expect } from "vitest";
/* Read by path rather than through the package's exports map, which a `?raw`
   query does not survive. */
import rawCss from "../../../packages/shared/css/tokens.css?raw";

/** The declarations of the first rule after `anchor`, keyed by exact property
 *  name. Good enough for flat, hand-authored blocks with comments stripped. */
export function declarations(css: string, anchor: string): Record<string, string> {
  const at = css.indexOf(anchor);
  expect(at, `no rule at ${anchor}`).toBeGreaterThan(-1);
  const open = css.indexOf("{", at);
  const found: Record<string, string> = {};
  for (const line of css.slice(open + 1, css.indexOf("}", open)).split(";")) {
    const [name, ...value] = line.split(":");
    if (name.trim()) found[name.trim()] = value.join(":").trim();
  }
  return found;
}

const css = rawCss.replace(/\/\*[\s\S]*?\*\//g, "");

/** One token's value in a theme, read out of the stylesheet. */
function token(anchor: string, name: string): string {
  const value = declarations(css, anchor)[name];
  expect(value, `${name} not declared under ${anchor}`).toBeDefined();
  return value;
}

/* Each palette block carries several selectors, one per surface, so these
   anchor on the selector this console is drawn by rather than on the whole
   list. */
export const TOKENS = {
  light: (name: string) => token(":root,", name),
  dark: (name: string) => token(':root[data-theme="dark"]', name),
} as const;
