import { describe, it, expect } from "vitest";
import rules from "../App.css?raw";

function body(selector: string): string {
  const start = rules.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no ${selector} rule in App.css`);
  const block = rules.slice(start);
  return block.slice(0, block.indexOf("}"));
}

/** The account menu drops from the header over whatever page is below it, and
 *  the map is the user surface's front page: its toolbar and Leaflet's controls
 *  float at z-index 1000 and above. The page keeps its layers to itself, so
 *  the menu clears it with a z-index of its own however high the page climbs. */
describe("the account menu over a page", () => {
  it("sits over a page that stacks on its own", () => {
    expect(body(".content")).toMatch(/isolation:\s*isolate/);
  });

  it("rises above the page's layer", () => {
    const z = Number(body(".user-dropdown").match(/z-index:\s*(\d+)/)?.[1]);
    expect(z).toBeGreaterThan(0);
  });
});
