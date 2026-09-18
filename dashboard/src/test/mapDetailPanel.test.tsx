import { describe, it, expect } from "vitest";
import rules from "../pages/map/LiveAircraftMap.css?raw";

/** The panel describes an aircraft on the map. Floating over the canvas hides
 *  the thing it is describing, which is tolerable at 1440 and is not once the
 *  console has taken its rail. */
describe(".detail-panel", () => {
  const block = rules.slice(rules.indexOf(".detail-panel {"));
  const body = block.slice(0, block.indexOf("}"));

  it("is not absolutely positioned", () => {
    expect(body).not.toMatch(/position:\s*absolute/);
  });

  it("carries no z-index or shadow, having nothing to sit above", () => {
    expect(body).not.toMatch(/z-index/);
    expect(body).not.toMatch(/box-shadow/);
  });

  it("keeps its width and its left border", () => {
    expect(body).toMatch(/width:\s*300px/);
    expect(body).toMatch(/border-left/);
  });

  it("stays a flex column, so its header holds while its body scrolls", () => {
    expect(body).toMatch(/display:\s*flex/);
    expect(body).toMatch(/flex-direction:\s*column/);
  });
});
