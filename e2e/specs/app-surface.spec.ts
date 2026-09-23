/**
 * The consolidated surface: the console at the root of the app hostname,
 * opening on its map.
 *
 * What a browser adds over the smoke tests is that the bundle executes and the
 * router lands where it should. The arrival at /map is react-router's own
 * redirect, so it needs the JavaScript to have loaded and run.
 *
 * Skipped where hosts.app is null — see the table in playwright.config.ts for
 * why production and the dev server are.
 */
import { test, expect } from "@playwright/test";
import { hosts } from "../playwright.config";

const APP = hosts.app;

test.skip(APP === null, "no consolidated app surface in this environment");

// Only ever read inside a test body. test.skip aborts the tests, not this
// module: every top-level statement here still runs while the file is being
// collected, so touching this at module scope throws on the environments where
// it is null and takes the whole run down with it.
const BASE = APP as string;

// A hostname is mostly dots, and an unescaped one matches any character.
function originPattern(): string {
  return BASE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

test.describe("the consolidated app surface", () => {
  test("opens on the map", async ({ page }) => {
    await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(new RegExp(`^${originPattern()}/map`));
    await expect(page.locator(".connection-badge")).toBeVisible({ timeout: 30_000 });
  });

  test("sends a private page to the login card", async ({ page }) => {
    await page.goto(`${BASE}/overview`, { waitUntil: "domcontentloaded" });
    await expect(page.locator(".login-card")).toBeVisible({ timeout: 30_000 });
    await expect(page).toHaveURL(new RegExp(`^${originPattern()}/login/?$`));
  });

  test("opens the explorer on the filters its link carries", async ({ page }) => {
    await page.goto(`${BASE}/data?from=2026-09-01&to=2026-09-03`, { waitUntil: "domcontentloaded" });
    // The page renders its shareable link from the filters it read, so this
    // needs the bundle to have run.
    await expect(page.getByTestId("de-share")).toContainText("from=2026-09-01&to=2026-09-03", {
      timeout: 30_000,
    });
  });
});
