import { describe, it, expect } from "vitest";
import css from "./LiveAircraftMap.css?raw";

const SELECTOR = ".aircraft-list-panel.collapsed .al-title";
const PHONE = "@media (max-width: 640px)";

/** The text between the brace that opens after `from` and its partner. */
function braced(text: string, from: number): { inner: string; end: number } {
  const open = text.indexOf("{", from);
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}" && --depth === 0) return { inner: text.slice(open + 1, i), end: i + 1 };
  }
  throw new Error(`unbalanced braces after ${from}`);
}

const phoneStart = css.indexOf(PHONE);
const phone = braced(css, phoneStart);
const outsidePhone = css.slice(0, phoneStart) + css.slice(phone.end);

/** The declarations of the rule whose selector is exactly `selector`. */
function declarations(text: string, selector: string): string | undefined {
  const match = text.match(new RegExp(`(?:^|[}\\s])${selector.replace(/\./g, "\\.")}\\s*\\{([^}]*)\\}`));
  return match?.[1];
}

describe("the collapsed list's title", () => {
  it("has a phone block", () => {
    expect(phoneStart).toBeGreaterThanOrEqual(0);
  });

  it("is hidden outside the phone block, so the desktop rail does not announce it", () => {
    expect(declarations(outsidePhone, SELECTOR)).toMatch(/display:\s*none/);
  });

  it("is shown in the phone block, where it labels the chip that opens the list", () => {
    expect(declarations(phone.inner, SELECTOR)).toMatch(/display:\s*flex/);
  });
});
