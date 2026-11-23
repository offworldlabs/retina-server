import { describe, it, expect } from "vitest";
import css from "./PhysicsSettings.css?raw";

const touch = "@media (max-width: 640px), (max-width: 1023.98px) and (pointer: coarse)";

/** The text between the braces that open at `start` and close with them. */
function block(text: string, start: number): string {
  const open = text.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < text.length; i++) {
    if (text[i] === "{") depth++;
    else if (text[i] === "}" && --depth === 0) return text.slice(open + 1, i);
  }
  return text.slice(open + 1);
}

/** A rule's declarations, found by its exact selector line. */
function body(text: string, selector: string): string {
  const at = text.indexOf(`${selector} {`);
  expect(at, `${selector} is missing`).toBeGreaterThanOrEqual(0);
  return block(text, at);
}

describe("the physics sliders on a touch screen", () => {
  it("stack their grids at the shared phone width", () => {
    expect(css).not.toContain("@media (max-width: 600px)");
    const phone = block(css, css.indexOf("@media (max-width: 640px) {"));
    expect(phone).toContain(".ps-settings-grid {");
    expect(phone).toContain(".ps-count-grid {");
    expect(phone).toContain(".ps-perf-grid {");
  });

  it("take a 44px band and draw the 5px track inside it", () => {
    const at = css.indexOf(touch);
    expect(at, "the touch block is missing").toBeGreaterThanOrEqual(0);
    const band = block(css, at);
    expect(body(band, ".ps-range")).toMatch(/height:\s*44px/);
    expect(body(band, ".ps-range::-webkit-slider-runnable-track")).toMatch(/height:\s*5px/);
    expect(body(band, ".ps-range::-moz-range-track")).toMatch(/height:\s*5px/);
  });

  it("keep the 5px slider on desktop", () => {
    const outside = css.slice(0, css.indexOf("@media"));
    expect(body(outside, ".ps-range")).toMatch(/height:\s*5px/);
  });
});

describe("the live feed's switch on a touch screen", () => {
  it("is a row as tall as a finger's target, and keeps its own height on desktop", () => {
    const band = block(css, css.indexOf(touch));
    expect(body(band, ".ps-toggle-row")).toMatch(/min-height:\s*44px/);
    const outside = css.slice(0, css.indexOf("@media"));
    expect(body(outside, ".ps-toggle-row")).not.toMatch(/min-height/);
  });
});
