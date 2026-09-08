/**
 * Dashboard (dash.retina.fm / staging-dash.retina.fm) E2E tests.
 *
 * The dashboard has two legitimate auth modes, and the server says which one
 * it is in on every unauthenticated GET /api/auth/me:
 *
 *   oauth   — OAuth client keys are configured. /api/auth/me answers 401,
 *             `/` redirects to /login, and /login renders the login card.
 *   bypass  — AUTH_ALLOW_ANONYMOUS_ADMIN=1 with no OAuth client (deployed in
 *             every environment while OAuth is unconfigured; see
 *             backend/.env.example). /api/auth/me answers 200 with the anonymous
 *             admin and `auth_enabled: false`. `/` renders the dashboard
 *             directly, and /login is a transient page: LoginPage navigates to
 *             `/` the moment the auth call resolves, so it shows the login card
 *             only for the one round trip to /api/auth/me.
 *
 * The login-card tests used to assume the oauth mode on /login and passed in
 * bypass mode only by racing that round trip — reliably from a GitHub runner,
 * 19 times in 20 failing from a client close to the origin. A lost race on
 * production rolls production back (ci.yml, e2e-prod). So: the tests that
 * check the real deployment key off the mode the server reports, and the tests
 * that check the login card's markup hold the auth call open (see
 * holdAuthUnresolved) so the card stays put while it is inspected.
 *
 * Authenticated flows are covered via API-level assumptions (see api.spec.ts).
 */
import { test, expect, request as playwrightRequest, type Page } from "@playwright/test";
import { hosts } from "../playwright.config";

const DASH = hosts.dash;
const API = hosts.api;

type AuthMode = "oauth" | "bypass";

/** Ask the server which auth mode it is in, exactly as the SPA does. */
async function serverAuthMode(): Promise<AuthMode> {
  const ctx = await playwrightRequest.newContext();
  const res = await ctx.get(`${DASH}/api/auth/me`);
  const status = res.status();
  const body = status === 200 ? await res.json() : null;
  await ctx.dispose();
  if (status === 401) return "oauth";
  if (status === 200 && body && body.auth_enabled === false) return "bypass";
  // A 200 that claims auth is enabled would be a leaked session on an
  // unauthenticated request; anything else is a broken auth endpoint. Both
  // are exactly what this suite exists to catch.
  throw new Error(`Unexpected unauthenticated /api/auth/me: ${status} ${JSON.stringify(body)}`);
}

/**
 * Keep the SPA's auth call from ever resolving, so LoginPage renders the login
 * card and stays on it whatever mode the server is in. The AuthProvider retries
 * a failed /api/auth/me four times with backoff (~9 s) before settling on "no
 * user", and LoginPage shows the card for as long as there is no user, so the
 * card is stable for the whole test. Aborting rather than answering 401 matters:
 * the API client answers a 401 by navigating to /login, which on /login is a
 * reload, and the page would never settle.
 */
async function holdAuthUnresolved(page: Page) {
  await page.route("**/api/auth/me", (route) => route.abort("connectionrefused"));
}

test.describe("Dashboard — unauthenticated access (real auth mode)", () => {
  test("/ renders what the server's auth mode says it should", async ({ page }) => {
    const mode = await serverAuthMode();
    await page.goto(DASH);
    if (mode === "oauth") {
      await page.waitForURL(/\/login/, { timeout: 10_000 });
      await expect(page.locator(".login-card")).toBeVisible({ timeout: 5_000 });
    } else {
      // Anonymous admin: the dashboard itself, not the login card, and not a
      // bare error page.
      await expect(page.locator("h1")).toBeVisible({ timeout: 10_000 });
      await expect(page.locator(".login-card")).toBeHidden();
      expect(page.url()).not.toMatch(/\/login/);
    }
  });

  test("/login resolves to the state the server's auth mode implies", async ({ page }) => {
    const mode = await serverAuthMode();
    await page.goto(`${DASH}/login`);
    if (mode === "oauth") {
      await expect(page.locator(".login-card")).toBeVisible({ timeout: 10_000 });
      await expect(page.locator("h1")).toContainText(/Retina/i);
    } else {
      // The anonymous admin is already "logged in": /login must hand over to
      // the dashboard, not strand the user on a login card that goes nowhere.
      await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 10_000 });
      await expect(page.locator("h1")).toBeVisible({ timeout: 10_000 });
      await expect(page.locator(".login-card")).toBeHidden();
    }
  });

  test("no JavaScript errors on login page load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto(`${DASH}/login`);
    await page.waitForLoadState("networkidle");
    expect(errors).toHaveLength(0);
  });
});

test.describe("Dashboard — login card (auth call held open)", () => {
  test.beforeEach(async ({ page }) => {
    await holdAuthUnresolved(page);
  });

  test("login page renders logo and title", async ({ page }) => {
    await page.goto(`${DASH}/login`);
    await expect(page.locator(".login-card")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator("h1")).toContainText(/Retina/i);
  });

  test("login page shows Google login button", async ({ page }) => {
    await page.goto(`${DASH}/login`);
    const googleLink = page.getByRole("link", { name: /Google/i });
    await expect(googleLink).toBeVisible({ timeout: 10_000 });
  });

  test("login page link points to /api/auth/login/google", async ({ page }) => {
    await page.goto(`${DASH}/login`);
    const googleLink = page.getByRole("link", { name: /Google/i });
    const href = await googleLink.getAttribute("href");
    expect(href).toMatch(/\/api\/auth\/login\/google/);
  });

  test("login page shows error message on ?error= query param", async ({ page }) => {
    await page.goto(`${DASH}/login?error=access_denied`);
    await expect(page.locator(".login-error")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(".login-error")).toContainText(/access denied/i);
  });
});

test.describe("Dashboard — admin API backing (no auth required)", () => {
  test("GET /api/admin/leaderboard returns nodes array", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(`${API}/api/admin/leaderboard`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    // Response shape: {leaderboard: [...], total: N}
    expect(body).toHaveProperty("leaderboard");
    expect(Array.isArray(body.leaderboard)).toBe(true);
    await ctx.dispose();
  });

  test("GET /api/admin/events returns event list", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(`${API}/api/admin/events`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    // Events response is an array or an object containing an events key
    const isValid = Array.isArray(body) || (typeof body === "object" && body !== null);
    expect(isValid).toBe(true);
    await ctx.dispose();
  });

  test("GET /api/admin/storage returns file_count and total_size_mb", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(`${API}/api/admin/storage`);
    // 202 = storage scan still in progress (valid startup state)
    expect([200, 202]).toContain(res.status());
    if (res.status() === 200) {
      const body = await res.json();
      expect(body).toHaveProperty("archive_files");
      expect(body).toHaveProperty("archive_bytes");
      expect(body).toHaveProperty("archive_mb");
      expect(typeof body.archive_files).toBe("number");
      expect(typeof body.archive_mb).toBe("number");
    }
    await ctx.dispose();
  });

  // On DASH, not API: the dashboard vhost proxies /api/config to
  // tower-finder-service (snippets/towers-proxy.conf), while the api vhost has
  // no such location and the app behind it no longer implements the route — the
  // monolith's tower stack went with the proxy dedup, so API would 404 here.
  test("GET /api/config returns a non-empty config object", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(`${DASH}/api/config`);
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(typeof body).toBe("object");
    expect(Object.keys(body).length).toBeGreaterThan(0);
    await ctx.dispose();
  });
});

test.describe("Dashboard — static asset delivery", () => {
  test("index.html is served with no-store Cache-Control", async () => {
    const ctx = await playwrightRequest.newContext();
    const res = await ctx.get(DASH);
    expect([200, 301, 302]).toContain(res.status());
    // Follow to login page
    const loginRes = await ctx.get(`${DASH}/login`);
    expect(loginRes.status()).toBe(200);
    const cacheHeader = loginRes.headers()["cache-control"] ?? "";
    // index.html should prevent browser caching to avoid stale bundle issues
    expect(cacheHeader).toMatch(/no-store|no-cache/i);
    await ctx.dispose();
  });
});
