/**
 * The consolidated surface: two bundles on one hostname.
 *
 * What a browser adds over the smoke tests is that the mounted bundle actually
 * executes. `/dash/` answered 200 for the whole of #426 while rendering nothing,
 * because the dashboard was built for a vhost root and every asset URL in its
 * index.html resolved against this vhost's root instead — the map's directory.
 * A status check cannot see that, and neither can a fetch of the HTML.
 *
 * So the assertion is a rendered element and the URL the router settled on. Both
 * require the JavaScript to have loaded and run: the redirect to the login page
 * is react-router's, and it carries the mount prefix only if the basename Vite
 * baked in came through with it.
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
  test("serves the map at the root", async ({ page }) => {
    await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
    // The map's own chrome, so this cannot pass on the dashboard bundle.
    await expect(page.locator(".connection-badge")).toBeVisible({ timeout: 30_000 });
  });

  test("runs the dashboard bundle under /dash/, opening on its own map", async ({ page }) => {
    await page.goto(`${BASE}/dash/`, { waitUntil: "domcontentloaded" });

    // The console's index forwards to its map, and that redirect is
    // react-router's, so reaching /dash/map proves the bundle's assets resolved,
    // it executed, and the router kept the mount. Without the basename this
    // would be `/map`, which on this vhost is the other bundle.
    await expect(page).toHaveURL(new RegExp(`^${originPattern()}/dash/map`));
    await expect(page.locator(".connection-badge")).toBeVisible({ timeout: 30_000 });
  });

  test("still sends a private page to the login card under /dash/", async ({ page }) => {
    await page.goto(`${BASE}/dash/overview`, { waitUntil: "domcontentloaded" });
    await expect(page.locator(".login-card")).toBeVisible({ timeout: 30_000 });
    await expect(page).toHaveURL(new RegExp(`^${originPattern()}/dash/login/?$`));
  });

  test("redirects the slashless /dash, keeping the query string", async ({ page }) => {
    await page.goto(`${BASE}/dash?next=nodes`, { waitUntil: "domcontentloaded" });
    // `/dash` does not match the `/dash/` mount, so without its own exact-match
    // redirect it falls through to the map. A `return` would drop the query
    // string; the template keeps it with $is_args$args.
    expect(page.url()).toContain("/dash/");
    expect(page.url()).toContain("next=nodes");
  });

  test("sends an old /data/ link to the dashboard's explorer, filters intact", async ({ page }) => {
    await page.goto(`${BASE}/data/?from=2026-09-01&to=2026-09-03`, { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(new RegExp(`^${originPattern()}/dash/data\\?`));
    // The page renders its shareable link from the filters it read, so this
    // needs the bundle to have run and the query to have survived the redirect.
    await expect(page.getByTestId("de-share")).toContainText("from=2026-09-01&to=2026-09-03", {
      timeout: 30_000,
    });
  });
});
