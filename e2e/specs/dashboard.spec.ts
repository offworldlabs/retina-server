/**
 * Dashboard E2E tests, against the `/dash/` mount on the app hostname.
 *
 * The dashboard has two legitimate auth modes, and the server says which one
 * it is in on every unauthenticated GET /api/auth/me:
 *
 *   oauth   — auth is enforced. /api/auth/me answers 401, a private page
 *             such as /overview redirects to /login, and /login renders the
 *             login card. (`/` is public: it forwards to the map.) Every deployed
 *             environment is in this mode; the name predates Cloudflare Access,
 *             and on a droplet it is a verified Access assertion rather than an
 *             OAuth client that satisfies it.
 *   bypass  — AUTH_ALLOW_ANONYMOUS_ADMIN=1 with no OAuth client, which only
 *             docker-compose.local.yml sets now (see backend/.env.example).
 *             /api/auth/me answers 200 with the anonymous
 *             admin and `auth_enabled: false`. A private page renders
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
import { hosts, dashBase } from "../playwright.config";

// The origin the dashboard is served from, and the base its pages sit at on it.
// A page route takes the mount; a same-origin API call does not, because the
// API is the vhost's rather than the bundle's.
const DASH = hosts.dash;
const DASH_PAGE = `${DASH}${dashBase}`;
const LOGIN_PATH = `${dashBase}/login`;
// A page that needs a session. The index does not: it forwards to the map.
const PRIVATE_PAGE = `${DASH_PAGE}/overview`;
const ADMIN = hosts.admin;
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

/**
 * The strongest claim the server's auth mode allows about the surface on screen.
 *
 * Past the login card the sidebar names the surface. Short of it both hostnames
 * render the same card, and reaching that card is itself the assertion: the host
 * resolved, Cloudflare Access admitted this run, and nginx served the dashboard
 * bundle rather than an edge error or a redirect loop.
 *
 * The origin is checked, not just the path. Cloudflare's own login page is
 * `<team>.cloudflareaccess.com/cdn-cgi/access/login/<host>`, so a bare /login
 * match is satisfied by the very page a run without a service token gets stuck
 * on, and the failure would read as a missing login card rather than as never
 * having been let in.
 */
async function expectSurface(page: Page, base: string, mode: AuthMode, name: string) {
  if (mode === "oauth") {
    // `base` carries the mount as well as the origin, so the login path is
    // derived from it rather than assumed to be at the root: on the app
    // hostname the dashboard's own /login is /dash/login.
    const { origin, pathname } = new URL(base);
    const login = `${pathname.replace(/\/$/, "")}/login`;
    await page.waitForURL((url) => url.origin === origin && url.pathname.startsWith(login), {
      timeout: 10_000,
    });
    await expect(page.locator(".login-card")).toBeVisible({ timeout: 5_000 });
    return;
  }
  await expect(page.locator(".brand-sub")).toHaveText(name, { timeout: 10_000 });
}

test.describe("Dashboard — unauthenticated access (real auth mode)", () => {
  test("a private page renders what the server's auth mode says it should", async ({ page }) => {
    const mode = await serverAuthMode();
    await page.goto(PRIVATE_PAGE);
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
    await page.goto(`${DASH_PAGE}/login`);
    if (mode === "oauth") {
      await expect(page.locator(".login-card")).toBeVisible({ timeout: 10_000 });
      await expect(page.locator("h1")).toContainText(/Retina/i);
    } else {
      // The anonymous admin is already "logged in": /login must hand over to
      // the dashboard, not strand the user on a login card that goes nowhere.
      await page.waitForURL((url) => !url.pathname.startsWith(LOGIN_PATH), { timeout: 10_000 });
      await expect(page.locator("h1")).toBeVisible({ timeout: 10_000 });
      await expect(page.locator(".login-card")).toBeHidden();
    }
  });

  test("no JavaScript errors on login page load", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    await page.goto(`${DASH_PAGE}/login`);
    await page.waitForLoadState("networkidle");
    expect(errors).toHaveLength(0);
  });
});

/**
 * Whether a hostname resolves at all, as distinct from what it answers.
 *
 * This suite runs inside the `staging` job that deploy-production needs, exactly
 * as the smoke tests do, so it can block a release for the same reason they can.
 * They report an unresolvable name as a warning rather than a failure, because
 * staging-admin.retina.fm's record is young and nothing monitors it; without
 * the same tolerance here a DNS wobble still holds up every deploy, through the
 * sibling gate.
 *
 * Narrow on purpose. Only a name that does not resolve is tolerated: any
 * response, a 5xx included, means the vhost is reachable and the test should
 * assert on what came back.
 */
async function resolves(url: string): Promise<boolean> {
  const ctx = await playwrightRequest.newContext();
  try {
    await ctx.get(url, { timeout: 15_000 });
    return true;
  } catch (err) {
    // Node reports DNS failure on the cause, not the message ("fetch failed").
    let text = "";
    for (let e: unknown = err, depth = 0; e && depth < 5; depth++) {
      text += String(e);
      e = (e as { cause?: unknown }).cause;
    }
    if (/ENOTFOUND|EAI_AGAIN|getaddrinfo/i.test(text)) {
      console.warn(`WARN ${url} does not resolve, so the admin surface went untested.`);
      return false;
    }
    return true;
  } finally {
    await ctx.dispose();
  }
}

test.describe("Admin surface selection", () => {
  // The whole point of the admin vhost: the same bundle the app hostname
  // mounts at /dash/, with a different route table, chosen client-side from the
  // hostname. Null on prod (see playwright.config.ts) so a wobble here cannot
  // roll production back.
  test.skip(!ADMIN, "no admin surface on this environment");

  // The mode is a property of the deployment, not of either test. beforeEach
  // runs per test, so cache it; the skip itself has to stay in beforeEach,
  // which is where Playwright accepts it. The value is cached rather than the
  // promise, so a request that fails leaves the next test free to try again
  // instead of inheriting a rejection.
  let authMode: AuthMode | undefined;
  let adminResolves: boolean | undefined;
  test.beforeEach(async () => {
    adminResolves ??= await resolves(ADMIN!);
    test.skip(!adminResolves, `${ADMIN} does not resolve`);
    authMode ??= await serverAuthMode();
  });

  // Separate tests, not two assertions in one: the /dash/ half is the control
  // that tells "admin selection broke" apart from "the sidebar markup changed
  // and both are wrong", and a shared body would stop at the first failure.
  //
  // Which surface a hostname selects is resolveSurface's answer, and is covered
  // exhaustively in dashboard/src/test/surface.test.ts. What only an end-to-end
  // run can show is that the vhost is reachable and serving this bundle, which
  // is why these stay here once enforced auth puts a login card in the way.
  test("the admin vhost serves the admin console", async ({ page }) => {
    await page.goto(ADMIN!);
    await expectSurface(page, ADMIN!, authMode!, "Admin Console");
  });

  test("the /dash/ mount serves the user dashboard", async ({ page }) => {
    await page.goto(PRIVATE_PAGE);
    await expectSurface(page, DASH_PAGE, authMode!, "Node Dashboard");
  });
});

test.describe("Dashboard — login card (auth call held open)", () => {
  test.beforeEach(async ({ page }) => {
    await holdAuthUnresolved(page);
  });

  test("login page renders logo and title", async ({ page }) => {
    await page.goto(`${DASH_PAGE}/login`);
    await expect(page.locator(".login-card")).toBeVisible({ timeout: 10_000 });
    await expect(page.locator("h1")).toContainText(/Retina/i);
  });

  test("login page offers sign-in by email", async ({ page }) => {
    await page.goto(`${DASH_PAGE}/login`);
    await expect(page.locator("#login-email")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole("button", { name: /sign-in link/i })).toBeVisible();
  });
});

// On API deliberately. api.retina.fm carries no Access application and never
// can: it is the fleet's ingest hostname, and a node cannot complete an
// interactive login. So the refusal here comes from require_admin in this
// codebase rather than from the edge, which is the point of enforcing
// backend-side — the Host header stops mattering.
//
// Response shape is no longer assertable from here, because nothing in CI can
// authenticate against this hostname. The backend suite covers it.
test.describe("Dashboard — admin API refuses anonymous callers", () => {
  // /api/admin/leaderboard is deliberately absent: it is the one route under
  // this prefix that answers anyone, and api.spec.ts asserts what it publishes.
  // What matters here is that opening it opened one route and not the prefix.
  for (const path of ["/api/admin/events", "/api/admin/storage", "/api/admin/node-refs"]) {
    test(`GET ${path} refuses an anonymous caller`, async () => {
      const ctx = await playwrightRequest.newContext();
      const res = await ctx.get(`${API}${path}`);
      expect(res.status()).toBe(401);
      await ctx.dispose();
    });
  }

  // On the dashboard's own origin, not API: the app vhost proxies /api/config
  // to tower-finder-service (snippets/towers-proxy.conf), while the api vhost has
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
    const res = await ctx.get(DASH_PAGE);
    expect([200, 301, 302]).toContain(res.status());
    // Follow to login page
    const loginRes = await ctx.get(`${DASH_PAGE}/login`);
    expect(loginRes.status()).toBe(200);
    const cacheHeader = loginRes.headers()["cache-control"] ?? "";
    // index.html should prevent browser caching to avoid stale bundle issues
    expect(cacheHeader).toMatch(/no-store|no-cache/i);
    await ctx.dispose();
  });
});
