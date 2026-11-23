import { describe, it, expect } from "vitest";
import bundle from "../../public/vendor/scalar-api-reference-1.69.0/standalone.js?raw";
import tablesCss from "../utils/scalarTables.css?raw";
import { SCALAR_THEME_CSS } from "../utils/scalarTheme";
import { TOKENS, declarations } from "./paletteTokens";

/** The palette token each of Scalar's colours stands for, in both themes. */
const TOKEN_FOR: Record<string, string> = {
  "--scalar-background-1": "--bg-primary",
  "--scalar-background-2": "--bg-secondary",
  "--scalar-background-3": "--bg-card-hover",
  "--scalar-background-card": "--bg-card",
  "--scalar-background-accent": "--accent-light",
  "--scalar-background-alert": "--warning-light",
  "--scalar-background-danger": "--error-light",
  "--scalar-border-color": "--border",
  "--scalar-color-1": "--text-primary",
  "--scalar-color-2": "--text-secondary",
  "--scalar-color-3": "--text-muted",
  "--scalar-color-accent": "--accent",
  "--scalar-color-green": "--success",
  "--scalar-color-red": "--error",
  "--scalar-color-orange": "--warning",
  "--scalar-color-blue": "--accent",
  "--scalar-link-color": "--accent",
  "--scalar-link-color-hover": "--accent-hover",
  "--scalar-button-1": "--accent",
  "--scalar-button-1-color": "--accent-ink",
  "--scalar-button-1-hover": "--accent-hover",
  "--scalar-header-background-1": "--bg-primary",
  "--scalar-header-background-2": "--bg-secondary",
  "--scalar-header-color-1": "--text-primary",
  "--scalar-header-color-2": "--text-secondary",
  "--scalar-header-border-color": "--border",
  "--scalar-header-call-to-action-color": "--accent",
  "--scalar-sidebar-background-1": "--bg-secondary",
  "--scalar-sidebar-border-color": "--border",
  "--scalar-sidebar-color-1": "--text-primary",
  "--scalar-sidebar-color-2": "--text-secondary",
  "--scalar-sidebar-color-active": "--accent",
  "--scalar-sidebar-item-hover-background": "--accent-tint",
  "--scalar-sidebar-item-hover-color": "--text-primary",
  "--scalar-sidebar-item-active-background": "--accent-light",
  "--scalar-sidebar-search-background": "--bg-input",
  "--scalar-sidebar-search-border-color": "--border",
  "--scalar-sidebar-search-color": "--text-secondary",
};

describe.each(["light", "dark"] as const)("Scalar's %s palette", (theme) => {
  // Scalar cannot read the console's tokens (see scalar-theme.css), so a token
  // that changes reaches it only through this.
  it("is the console's, token for token", () => {
    const expected = Object.fromEntries(Object.entries(TOKEN_FOR).map(([name, token]) => [name, TOKENS[theme](token)]));
    expect(declarations(SCALAR_THEME_CSS, `.${theme}-mode {`)).toEqual(expected);
  });
});

describe("the variables themed", () => {
  // A Scalar that no longer reads one of these names drops that colour without
  // an error, so an upgrade of the vendored bundle is checked here.
  it("are all read by the vendored Scalar", () => {
    const unread = Object.keys(TOKEN_FOR).filter((name) => !new RegExp(`var\\(${name}[,)]`).test(bundle));
    expect(unread).toEqual([]);
  });
});

describe("the console's narrow-screen table rule", () => {
  it("keeps the first column of Scalar's markdown tables on one line below 1024px", () => {
    const block = /@media \(max-width: 1023\.98px\) \{([^]*)\}\s*$/.exec(SCALAR_THEME_CSS);
    expect(block).not.toBeNull();
    const rule = /([^{}]+)\{([^{}]*)\}/.exec(block![1])!;
    const selectors = rule[1].split(",").map((s) => s.trim());
    expect(selectors).toEqual([".scalar-app .markdown th:first-of-type", ".scalar-app .markdown td:first-of-type"]);
    expect(rule[2].trim()).toBe("white-space: nowrap;");
  });

  it("is media blocks and nothing else, so a desktop is untouched", () => {
    const outside = tablesCss
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/@media[^{]*\{(?:[^{}]*\{[^{}]*\})*\s*\}/g, "")
      .trim();
    expect(outside).toBe("");
  });
});
