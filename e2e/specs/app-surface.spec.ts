/**
 * The consolidated surface: the console at the root of the app hostname,
 * opening on its map.
 *
 * What a browser adds over the smoke tests is that the bundle executes and the
 * router lands where it should. The arrival at /map is react-router's own
 * redirect, so it needs the JavaScript to have loaded and run.
 *
 * Skipped where the app role is null — see the table in playwright.config.ts for
 * why production and the dev server are.
 */
import { test, expect } from "@playwright/test";
import { hostOrSkip } from "../playwright.config";

const APP = hostOrSkip("app", "no consolidated app surface in this environment");

// A hostname is mostly dots, and an unescaped one matches any character.
const ORIGIN = APP.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

test.describe("the consolidated app surface", () => {
  test("opens on the map", async ({ page }) => {
    await page.goto(`${APP}/`, { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(new RegExp(`^${ORIGIN}/map`));
    await expect(page.locator(".connection-badge")).toBeVisible({ timeout: 30_000 });
  });

  test("sends a private page to the login card", async ({ page }) => {
    await page.goto(`${APP}/overview`, { waitUntil: "domcontentloaded" });
    await expect(page.locator(".login-card")).toBeVisible({ timeout: 30_000 });
    await expect(page).toHaveURL(new RegExp(`^${ORIGIN}/login/?$`));
  });

  test("opens the explorer on the filters its link carries", async ({ page }) => {
    await page.goto(`${APP}/data?from=2026-09-01&to=2026-09-03`, { waitUntil: "domcontentloaded" });
    // The page renders its shareable link from the filters it read, so this
    // needs the bundle to have run.
    await expect(page.getByTestId("de-share")).toContainText("from=2026-09-01&to=2026-09-03", {
      timeout: 30_000,
    });
  });
});
