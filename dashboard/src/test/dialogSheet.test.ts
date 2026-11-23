import { describe, expect, it } from "vitest";
import dialogCss from "../components/dialog.css?raw";

/* On a phone the console's modal is a bottom sheet, drawn by the stylesheet
   alone: the component and its markup are the same at every width. Above a
   phone's width nothing changes, which the last test pins rule by rule. */

const bare = dialogCss.replace(/\/\*[\s\S]*?\*\//g, "");

/** Each top-level rule or at-rule in `css`, braces balanced. */
function blocks(css: string): { prelude: string; body: string; text: string }[] {
  const found: { prelude: string; body: string; text: string }[] = [];
  let at = 0;
  for (let open = css.indexOf("{"); open >= 0; open = css.indexOf("{", at)) {
    let close = open + 1;
    for (let depth = 1; depth > 0; close++) depth += css[close] === "{" ? 1 : css[close] === "}" ? -1 : 0;
    found.push({ prelude: css.slice(at, open).trim(), body: css.slice(open + 1, close - 1), text: css.slice(at, close) });
    at = close;
  }
  return found;
}

/** Every value `property` takes on `selector` inside `media`, in source order,
 *  so a fallback and the value after it both show. */
function values(media: string, selector: string, property: string): string[] {
  return blocks(bare)
    .filter((b) => b.prelude === media)
    .flatMap((b) => blocks(b.body))
    .filter((rule) => rule.prelude.split(",").map((s) => s.trim()).includes(selector))
    .flatMap((rule) => rule.body.split(";"))
    .map((line) => /^\s*([\w-]+)\s*:\s*([\s\S]+?)\s*$/.exec(line))
    .filter((m) => m?.[1] === property)
    .map((m) => m![2]);
}

const PHONE = "@media (max-width: 640px)";

describe("the dialog on a phone", () => {
  it("lies along the foot of the screen at its full width, rounded at the top", () => {
    expect(values(PHONE, ".dialog[open]", "place-items")).toEqual(["end stretch"]);
    expect(values(PHONE, ".dialog", "padding")).toEqual(["0 env(safe-area-inset-right) 0 env(safe-area-inset-left)"]);
    expect(values(PHONE, ".dialog-panel", "width")).toEqual(["100%"]);
    expect(values(PHONE, ".dialog-panel", "border-radius")).toEqual([
      "calc(1.5 * var(--radius)) calc(1.5 * var(--radius)) 0 0",
    ]);
  });

  // `vh` first, for a browser that does not know `dvh`.
  it("takes at most 90% of the visible height", () => {
    expect(values(PHONE, ".dialog-panel", "max-height")).toEqual(["90vh", "90dvh"]);
  });

  it("keeps its title and actions in view while the rest scrolls, and the scroll inside", () => {
    expect(values(PHONE, ".dialog-title", "position")).toEqual(["sticky"]);
    expect(values(PHONE, ".dialog-title", "top")).toEqual(["0"]);
    expect(values(PHONE, ".dialog-actions", "position")).toEqual(["sticky"]);
    expect(values(PHONE, ".dialog-actions", "bottom")).toEqual(["0"]);
    expect(values(PHONE, ".dialog-panel", "overscroll-behavior")).toEqual(["contain"]);
  });

  it("keeps its actions above the home indicator", () => {
    expect(values(PHONE, ".dialog-actions", "padding-bottom")).toEqual(["max(20px, env(safe-area-inset-bottom))"]);
  });
});

describe("the dialog above a phone's width", () => {
  // Everything outside a max-width query, whitespace collapsed: the centred
  // card a desktop and a tablet draw. A change here is a change to both.
  it("keeps the rules it has always had", () => {
    const rules = blocks(bare)
      .map((b) => b.text.replace(/\s+/g, " ").trim())
      .filter((text) => !/^@media[^{]*\(max-width/.test(text));
    expect(rules).toEqual([
      ".dialog { position: fixed; inset: 0; width: 100%; height: 100%; max-width: none; max-height: none; margin: 0; padding: 16px; border: none; background: var(--shadow); -webkit-backdrop-filter: blur(2px); backdrop-filter: blur(2px); color: var(--text-primary); font-size: 14px; line-height: 1.5; overscroll-behavior: contain; }",
      ".dialog[open] { display: grid; place-items: center; }",
      ".dialog::backdrop { background: transparent; }",
      ".dialog-panel { width: min(460px, 100%); max-height: 100%; overflow-y: auto; padding: 20px; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: 0 16px 40px var(--shadow); }",
      ".dialog-title { margin-bottom: 8px; font-size: 16px; font-weight: 600; line-height: 1.4; overflow-wrap: anywhere; }",
      ".dialog-description { color: var(--text-secondary); overflow-wrap: anywhere; }",
      ".dialog-body { display: flex; flex-direction: column; gap: 12px; margin-top: 16px; }",
      ".dialog-field { display: flex; flex-direction: column; gap: 6px; }",
      ".dialog-field label { font-size: 13px; font-weight: 500; color: var(--text-secondary); overflow-wrap: anywhere; }",
      ".dialog .input { width: 100%; font-size: 14px; }",
      ".dialog .notice { margin-bottom: 0; }",
      ".dialog-actions { justify-content: flex-end; margin-top: 20px; }",
      "@media (prefers-reduced-motion: no-preference) { .dialog[open] .dialog-panel { animation: dialog-rise 0.16s ease-out; } }",
      "@keyframes dialog-rise { from { opacity: 0; transform: translateY(8px); } }",
    ]);
  });
});
