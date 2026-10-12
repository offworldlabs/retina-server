import { describe, it, expect } from "vitest";
import appCss from "../App.css?raw";
import requireAuth from "../components/RequireAuth.tsx?raw";

/* The sign-in cards (the login page, the mailed link's page, a claim) are read
   on phones as often as at a desk, and their lines can carry an email address,
   which has no break point of its own. So can the admin console's Access
   Denied card. These read the stylesheet for the rules that keep both whole on
   a short, narrow screen. */

interface Rule {
  /** The media query the rule sits in, or null at the top level. */
  media: string | null;
  selectors: string[];
  /** In source order, so a fallback and the value after it both show. */
  declarations: [property: string, value: string][];
}

/** Every style rule in `css`, with the media query around it. Good enough for
 *  a hand-authored stylesheet with its comments stripped: braces nest only
 *  inside an at-rule, and the at-rules other than @media hold no rules this
 *  file reads. */
function parse(css: string, media: string | null = null): Rule[] {
  const found: Rule[] = [];
  let at = 0;
  for (;;) {
    const open = css.indexOf("{", at);
    if (open < 0) return found;
    let depth = 1;
    let close = open + 1;
    for (; depth > 0 && close < css.length; close++) {
      if (css[close] === "{") depth++;
      else if (css[close] === "}") depth--;
    }
    const prelude = css.slice(at, open).trim();
    const body = css.slice(open + 1, close - 1);
    if (prelude.startsWith("@media")) {
      found.push(...parse(body, prelude));
    } else if (!prelude.startsWith("@")) {
      found.push({
        media,
        selectors: prelude.split(",").map((s) => s.trim()),
        declarations: body
          .split(";")
          .map((line) => {
            const colon = line.indexOf(":");
            return [line.slice(0, colon).trim(), line.slice(colon + 1).trim()] as [string, string];
          })
          .filter(([property]) => property),
      });
    }
    at = close;
  }
}

const rules = parse(appCss.replace(/\/\*[\s\S]*?\*\//g, ""));

/** What every rule naming `selector`, alone or in a list, declares in `media`. */
function declared(selector: string, media: string | null = null): [string, string][] {
  return rules.filter((r) => r.media === media && r.selectors.includes(selector)).flatMap((r) => r.declarations);
}

/** The value `property` takes on `selector`: the last one given, as in CSS. */
function value(selector: string, property: string, media: string | null = null): string | undefined {
  return declared(selector, media)
    .filter(([p]) => p === property)
    .map(([, v]) => v)
    .pop();
}

/** A padding shorthand's four sides, in pixels. */
function sides(shorthand: string | undefined) {
  const [top, right = top, bottom = top, left = right] = (shorthand ?? "").split(/\s+/).map(parseFloat);
  return { top, right, bottom, left };
}

describe("the stylesheet parse", () => {
  it("finds rules, so a parse that matched nothing cannot pass", () => {
    expect(rules.length).toBeGreaterThan(100);
    expect(rules.some((r) => r.media !== null)).toBe(true);
  });
});

describe("the sign-in page", () => {
  it("is at least the screen's height, measured by the dynamic viewport where there is one", () => {
    const minHeights = declared(".login-page")
      .filter(([p]) => p === "min-height")
      .map(([, v]) => v);
    expect(minHeights).toEqual(["100vh", "100dvh"]);
  });

  // A fixed height cuts off a card taller than a phone's screen; a page that
  // only has a minimum grows with it.
  it("has no fixed height on any screen", () => {
    const everywhere = rules.filter((r) => r.selectors.includes(".login-page")).flatMap((r) => r.declarations);
    expect(everywhere.map(([p]) => p)).not.toContain("height");
    expect(everywhere.map(([p]) => p)).not.toContain("max-height");
  });

  // The page grows past the screen, so the document is what scrolls: nothing
  // between the two may hide what overflows.
  it.each([".login-page", "html", "body", "#root"])("lets %s scroll vertically", (selector) => {
    const overflow = declared(selector).filter(([p]) => /^overflow(-y)?$/.test(p));
    expect(overflow.filter(([, v]) => /hidden|clip/.test(v))).toEqual([]);
  });

  it("finds the document's own rules, so the sweep above cannot pass by matching nothing", () => {
    expect(value("#root", "height")).toBe("100%");
  });

  it("centres the card by its auto margins, which share out only the room there is", () => {
    expect(value(".login-card", "margin")).toBe("auto");
  });

  it("leaves room around the card when it reaches the screen's edges", () => {
    const padding = sides(value(".login-page", "padding"));
    expect(padding.top).toBeGreaterThan(0);
    expect(padding.bottom).toBeGreaterThan(0);
    expect(padding.left).toBe(16);
    expect(padding.right).toBe(16);
  });
});

describe("the sign-in card on a phone", () => {
  const narrow = rules.filter((r) => r.selectors.includes(".login-card") && /max-width:\s*\d+px/.test(r.media ?? ""));

  it("has a rule of its own for a narrow screen, which a 390px phone falls under", () => {
    expect(narrow).toHaveLength(1);
    expect(Number(narrow[0].media!.match(/max-width:\s*(\d+)px/)![1])).toBeGreaterThanOrEqual(390);
  });

  it("takes the width it is given, up to the width it has on a desk", () => {
    expect(value(".login-card", "width")).toBe("100%");
    expect(value(".login-card", "max-width")).toBe("400px");
    // A flex item is otherwise at least as wide as its longest unbroken word.
    expect(value(".login-card", "min-width")).toBe("0");
  });

  it("gives the sides less padding than on a desk", () => {
    const desk = sides(value(".login-card", "padding"));
    const phone = sides(value(".login-card", "padding", narrow[0].media));
    expect(phone.left).toBeLessThan(desk.left);
    expect(phone.right).toBeLessThan(desk.right);
  });

  // The Back control is placed inside the top padding, clear of the logo.
  it("keeps the top padding the Back control sits in", () => {
    const desk = sides(value(".login-card", "padding"));
    const phone = sides(value(".login-card", "padding", narrow[0].media));
    expect(phone.top).toBe(desk.top);
  });
});

describe("a long address on a sign-in card", () => {
  // Not `anywhere`, which the build's Safari floor predates.
  it.each([".login-card .subtitle", ".login-note", ".login-error", ".login-btn"])("breaks inside %s", (selector) => {
    expect(value(selector, "overflow-wrap")).toBe("break-word");
  });

  // A flex container's text is an anonymous item that will not shrink below
  // its longest word, whatever overflow-wrap says.
  it("breaks inside a button, which lays its label out as a block", () => {
    expect(value(".login-btn", "display")).toBe("block");
  });
});

describe("a link drawn as a sign-in button", () => {
  it("keeps the button's ink under the pointer, where a:hover would turn it accent", () => {
    expect(value("a:hover", "color")).toBe("var(--accent-hover)");
    expect(value(".login-btn:hover", "color")).toBe(value(".login-btn", "color"));
  });

  // The class outranks the bare `a` rule; only a link-state rule of the same
  // shape as a:hover would outrank it in turn.
  it("keeps it once visited", () => {
    const linkStates = rules.filter((r) => r.selectors.some((s) => /^a:(link|visited)\b/.test(s)));
    expect(linkStates).toEqual([]);
  });

  it("shows keyboard focus with the console's ring", () => {
    expect(value(".login-btn:focus-visible", "outline")).toBe("2px solid var(--accent)");
  });

  // A button takes the platform's control font, a link the page's.
  it("sets one face and one line height, so a link and a button on one card match", () => {
    expect(value(".login-btn", "font-family")).toBe("inherit");
    expect(value(".login-btn", "line-height")).toBeDefined();
  });
});

describe("the Access Denied card", () => {
  it("is the card RequireAuth draws", () => {
    expect(requireAuth).toContain('className="card access-denied-card"');
  });

  it("is held to a reading measure", () => {
    const width = parseFloat(value(".access-denied-card", "max-width") ?? "");
    expect(width).toBeGreaterThanOrEqual(480);
    expect(width).toBeLessThanOrEqual(720);
  });

  // The card hides its overflow, so an address that ran past its edge would be
  // cut off rather than spill.
  it("breaks a long address", () => {
    expect(value(".access-denied-card", "overflow-wrap")).toBe("break-word");
    // The stack sizes each line to its own content, which an unbroken address
    // would make wider than the card; held to the card's width, it breaks.
    expect(value(".access-denied-card .card-body > *", "max-width")).toBe("100%");
  });

  it("replaces the retired full-screen refusal, and the loading screen it shared a rule with stays", () => {
    expect(rules.filter((r) => r.selectors.includes(".access-denied"))).toEqual([]);
    expect(value(".loading-screen", "display")).toBe("flex");
  });
});
