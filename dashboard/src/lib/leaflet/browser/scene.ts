import type { Page } from "@playwright/test";
import type { Scene } from "./harness";

export const RED = "#ff0000";
export const BLUE = "#0000ff";

/** A 400x300 map at [10, 20], with the probe that publishes it, around `layers`. */
export const view = (...layers: Scene[]): Scene => [
  "MapContainer",
  { center: [10, 20], zoom: 10, style: { width: 400, height: 300 } },
  ["MapProbe", {}],
  ...layers,
];

export async function openHarness(page: Page) {
  await page.goto("/src/lib/leaflet/browser/harness.html");
  await page.waitForFunction(() => window.lf !== undefined);
}

// Scenes cross into the page untyped: Playwright's argument types recurse
// without end on a recursive tuple.
export async function render(page: Page, scene: Scene | null, strict = false) {
  await page.evaluate(([tree, inStrictMode]) => window.lf.render(tree as Scene, inStrictMode), [scene as unknown, strict] as const);
}

/** Renders `scene` and returns the Leaflet setters that render called. */
export async function settersCalledBy(page: Page, scene: Scene) {
  return page.evaluate((tree) => {
    window.lf.calls.length = 0;
    window.lf.render(tree as Scene);
    return [...window.lf.calls];
  }, scene as unknown);
}
