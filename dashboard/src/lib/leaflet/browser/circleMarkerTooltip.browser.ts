import { expect, test } from "@playwright/test";
import { BLUE, RED, openHarness, render, settersCalledBy, view } from "./scene";

test.beforeEach(async ({ page }) => {
  await openHarness(page);
  await page.evaluate(async (urls) => {
    const [{ CircleMarker }, { Tooltip }] = await Promise.all(urls.map((url) => import(url)));
    Object.assign(window.lf.types, { CircleMarker, Tooltip });
  }, ["/src/lib/leaflet/CircleMarker.tsx", "/src/lib/leaflet/Tooltip.tsx"]);
});

test("a CircleMarker top-level style prop change is applied with setStyle and keeps the layer", async ({ page }) => {
  const scene = (color: string) =>
    view(["CircleMarker", { center: [10, 20], radius: 8, className: "probe-dot", color, fillColor: color, weight: 2, ref: "dot" }]);
  const stamp = () => page.evaluate(() => window.lf.L.stamp(window.lf.refs.dot.current));
  await render(page, scene(RED));
  const before = await stamp();

  expect(await settersCalledBy(page, scene(BLUE))).toEqual(["dot.setStyle"]);
  await expect(page.locator("path.probe-dot")).toHaveAttribute("stroke", BLUE);
  await expect(page.locator("path.probe-dot")).toHaveAttribute("fill", BLUE);
  expect(await stamp()).toBe(before);
});

test("Tooltip content updates in place", async ({ page }) => {
  const scene = (text: string) =>
    view(["CircleMarker", { center: [10, 20], radius: 8 }, ["Tooltip", { direction: "top", permanent: true }, ["b", { className: "probe-tip" }, text]]]);
  const box = () => page.locator(".leaflet-tooltip").evaluate((el) => el.getBoundingClientRect().toJSON() as DOMRect);
  await render(page, scene("Hi"));
  await page.locator(".probe-tip").evaluate((el) => (el.dataset.kept = "yes"));
  const before = await box();

  await render(page, scene("A much longer line of tooltip text"));
  await expect(page.locator('.leaflet-tooltip .probe-tip[data-kept="yes"]')).toHaveText("A much longer line of tooltip text");
  const after = await box();
  expect(after.width).toBeGreaterThan(before.width + 100);
  // Re-laid out, so still centred over the marker as it grew.
  expect(Math.abs(after.x + after.width / 2 - (before.x + before.width / 2))).toBeLessThanOrEqual(1);
});
