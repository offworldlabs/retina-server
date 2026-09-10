/**
 * Live Aircraft Map (testmap domain) E2E tests.
 *
 * This suite visits whichever host `hosts.testmap` names — in CI that is
 * staging-map.retina.fm, not the public testmap.retina.fm, so the suite does not
 * depend on where the demo is currently served from. Both are the same surface:
 * the synthetic fleet, unfiltered. It verifies the map page loads, WebSocket
 * connects, aircraft appear, and key interactive elements work correctly.
 *
 * NOTE: These tests require the synthetic fleet to be running on the target
 * environment. They use generous timeouts to account for warm-up time.
 *
 * Production has no synthetic map surface — it runs no simulator, and
 * testmap.retina.fm is served by staging — so the whole file skips there rather
 * than reaching across environments. See the note in playwright.config.ts: a
 * failed production E2E auto-rolls-back production, so a suite that silently
 * tested staging could revert a good production build.
 */
import { test, expect, Page } from "@playwright/test";
import { hosts } from "../playwright.config";

const TESTMAP = hosts.testmap;

test.skip(
  TESTMAP === null,
  "no synthetic map surface in this environment (production runs no fleet)",
);

// Safe past the skip above, which aborts every test in the file when null.
const BASE = TESTMAP as string;

// Helper: wait for the connection badge to show "LIVE"
async function waitForLive(page: Page, timeoutMs = 15_000) {
  await expect(page.locator(".connection-badge")).toHaveText(/LIVE/i, {
    timeout: timeoutMs,
  });
}

/**
 * The unfiltered feed, which is what these surfaces render: usesRealOnlyFeed is
 * anchored to `^map\.`, which testmap is never. Unreadable counts as non-empty so
 * the caller's original failure stands.
 */
async function feedIsEmpty(page: Page): Promise<boolean> {
  try {
    // page.request, not a fresh context: this runs on a failure path that retries
    // twice, and the page already carries a connected one needing no teardown.
    // Explicitly bounded: page.request defaults to 30 s, and two of those would
    // consume the whole budget rowsOrSkip sets, turning a clean fail into a
    // generic test timeout. A snapshot this slow cannot excuse a skip anyway.
    const res = await page.request.get(`${hosts.api}/api/radar/data/aircraft.json`, {
      timeout: 5_000,
    });
    if (!res.ok()) return false;
    const body = await res.json();
    // A payload without the array is a broken feed, not an empty one, and must
    // fail rather than skip: the toolbar would read 0 under the same regression.
    return Array.isArray(body.aircraft) && body.aircraft.length === 0;
  } catch {
    return false;
  }
}

/**
 * Wait for aircraft rows, skipping if there is simply nothing to list: the fleet's
 * traffic comes and goes, and this suite gates the production deploy. Rows missing
 * while data was available still fails, which is the case worth catching.
 */
async function rowsOrSkip(page: Page) {
  // The empty-feed path is the slow one and the only one that can skip: up to
  // 15 s in waitForLive, then a 25 s row wait to completion, then two 5 s-capped
  // feed reads either side of a 3 s pause. That overruns the suite's 30 s
  // default, and a test timeout would kill the run before it could decide to
  // skip, so the budget travels with the helper that spends it.
  test.setTimeout(60_000);
  const rows = page.locator(".aircraft-list-panel .al-row");
  try {
    await expect(rows.first()).toBeVisible({ timeout: 25_000 });
  } catch (err) {
    // Only skip when nothing had any aircraft to show. The badge alone is not
    // enough: it reports socket connection, not delivery, so a socket that opened
    // and then went quiet still reads LIVE. Require the page's own count to agree
    // with an empty server snapshot, sampled twice across a push interval so one
    // unlucky instant does not excuse a real regression.
    const shown = await page
      .locator(".aircraft-count")
      .textContent()
      .catch(() => null);
    // Only a literal zero excuses a skip. Absent, blank or unparseable is a
    // toolbar that failed to render its count, not a page reporting no traffic,
    // and that has to fail like any other regression.
    const counted = /^\s*(\d+)/.exec(shown ?? "");
    const pageHasNone = counted !== null && Number(counted[1]) === 0;
    if (pageHasNone && (await feedIsEmpty(page))) {
      await new Promise((r) => setTimeout(r, 3_000));
      if (await feedIsEmpty(page)) test.skip(true, "aircraft feed is empty");
    }
    throw err;
  }
  return rows;
}

test.describe("Live Map — page identity", () => {
  test("page title contains RETINA", async ({ page }) => {
    await page.goto(BASE);
    // The HTML <title> is static "Tower Finder" for all domains;
    // the domain identity is exposed in the h1 element instead.
    await expect(page.locator("h1")).toContainText(/RETINA/i);
  });

  test("header shows RETINA, not Tower Finder", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator("h1")).not.toHaveText(/Tower Finder/i);
    await expect(page.locator("h1")).toContainText(/RETINA/i);
  });

  test("Live Radar tab is visible and active by default", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.getByRole("button", { name: /Live Radar/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Tower Search/i })).toBeHidden();
  });

  test("no JavaScript errors on page load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto(BASE);
    await page.waitForLoadState("networkidle");
    expect(errors).toHaveLength(0);
  });
});

test.describe("Live Map — map rendering", () => {
  test("Leaflet map container is present", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".leaflet-container")).toBeVisible({ timeout: 10_000 });
  });

  test("toolbar is rendered with connection badge", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".connection-badge")).toBeVisible();
  });

  test("toolbar shows Coverage / Labels / Trails toggle buttons", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole("button", { name: /Coverage/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Labels/i })).toBeVisible();
    await expect(page.getByRole("button", { name: /Trails/i })).toBeVisible();
  });

  test("Debug Truth toggle is present on testmap domain", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole("button", { name: /Debug Truth/i })).toBeVisible();
  });
});

test.describe("Live Map — WebSocket connectivity", { tag: "@live" }, () => {
  test("connection badge transitions to LIVE within 15s", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);
    await expect(page.locator(".connection-badge")).toHaveClass(/connected/);
  });

  test("aircraft count is non-empty once connected", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);
    await expect(page.locator(".aircraft-count")).toBeVisible();
  });

  test("Pause button toggles to Resume and back", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);

    const pauseBtn = page.getByRole("button", { name: /Pause/i });
    await expect(pauseBtn).toBeVisible();
    await pauseBtn.click();

    await expect(page.locator(".connection-badge")).toHaveText(/PAUSED/i);
    await expect(page.getByRole("button", { name: /Resume/i })).toBeVisible();

    await page.getByRole("button", { name: /Resume/i }).click();
    await expect(page.locator(".connection-badge")).toHaveText(/LIVE/i);
  });
});

test.describe("Live Map — aircraft list panel", { tag: "@live" }, () => {
  test("aircraft list panel renders within 20s of connection", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);

    // Panel should exist after aircraft start arriving
    await expect(page.locator(".aircraft-list-panel")).toBeVisible({ timeout: 20_000 });
  });

  test("aircraft list shows rows once data arrives", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);

    const rows = await rowsOrSkip(page);
    expect(await rows.count()).toBeGreaterThan(0);
  });

  test("clicking an aircraft row opens the detail panel", async ({ page }) => {
    await page.goto(BASE);
    await waitForLive(page);

    const row = (await rowsOrSkip(page)).first();
    await row.click();

    // Detail panel should open
    await expect(page.locator(".detail-panel").first()).toBeVisible({
      timeout: 5_000,
    });
  });
});

test.describe("Live Map — toolbar toggles", () => {
  test("Coverage toggle adds/removes active class", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });

    const btn = page.getByRole("button", { name: /Coverage/i });
    const initialActive = await btn.evaluate((el) => el.classList.contains("active"));

    await btn.click();
    const afterActive = await btn.evaluate((el) => el.classList.contains("active"));
    expect(afterActive).toBe(!initialActive);
  });

  test("Arcs toggle adds/removes active class", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });

    const btn = page.getByRole("button", { name: /Arcs/i });
    const initialActive = await btn.evaluate((el) => el.classList.contains("active"));

    await btn.click();
    const afterActive = await btn.evaluate((el) => el.classList.contains("active"));
    expect(afterActive).toBe(!initialActive);
  });

  test("Labels toggle adds/removes active class", async ({ page }) => {
    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });

    const btn = page.getByRole("button", { name: /Labels/i });
    const before = await btn.evaluate((el) => el.classList.contains("active"));
    await btn.click();
    const after = await btn.evaluate((el) => el.classList.contains("active"));
    expect(after).toBe(!before);
  });

  test("Fit button does not throw errors", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));

    await page.goto(BASE);
    await expect(page.locator(".live-map-toolbar")).toBeVisible({ timeout: 10_000 });

    const fitBtn = page.getByRole("button", { name: /Fit/i });
    await expect(fitBtn).toBeVisible();
    await fitBtn.click();

    await page.waitForTimeout(500);
    expect(errors).toHaveLength(0);
  });
});
