import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E test configuration.
 *
 * Environments (set via E2E_ENV):
 *   staging  → staging-api / staging-app / staging-admin (default)
 *   prod     → api / app (no synthetic map, no admin)
 *   local    → localhost:8000 (api) / localhost:5174 (the console, map included)
 *
 * The entries below are roles, not hostnames, which is why several of them hold
 * the same origin on a deployed environment: one hostname serves the console,
 * map included. They stay separate because they differ where it matters — on
 * production and on the dev server, where each answers its own question about
 * what exists.
 *
 * No entry names a towers hostname. Those are routed to tower-finder-service's
 * own edge by a Cloudflare Origin Rule, so nothing this repo builds answers
 * there: a test against one asserts another service's markup, and on prod a
 * failed E2E rolls production back.
 *
 * `testmap` names the environment whose console has a simulator behind its
 * /sim page — the surface is a path on the one console now, not a hostname of
 * its own, so this entry differs from `map` only in which origin is worth
 * asking. It is null on prod, and that is load-bearing rather than tidiness:
 * production runs no simulator, so /sim there is an empty map. Pointing the
 * production suite at staging's would mean the production E2E exercising
 * staging, and because a failed production E2E auto-rolls-back production
 * (ci.yml), a staging wobble would revert a good production build. The one
 * suite that needs the surface skips itself instead.
 */

const ENV = (process.env.E2E_ENV ?? "staging") as "staging" | "prod" | "local";

const HOSTS = {
  staging: {
    api:       "https://staging-api.retina.fm",
    // The consolidated surface, whose root opens on the live map.
    map:       "https://staging-app.retina.fm",
    // The console whose /sim page has a fleet behind it, which is what the
    // live-map suite needs. Staging runs one, so this is the same origin as
    // `map` above and always will be — one console per environment. The two
    // entries differ on production, where the fleet does not exist.
    testmap:   "https://staging-app.retina.fm",
    // The dashboard's origin.
    dash:      "https://staging-app.retina.fm",
    // Same bundle as dash, built at a root instead; the hostname is what selects
    // the admin route table. It keeps a name of its own because a Cloudflare
    // Access application can only be scoped to one.
    admin:     "https://staging-admin.retina.fm",
    // The consolidated surface as a whole, for the suite that asserts its
    // arrival and its old addresses' redirects rather than any one page.
    app:       "https://staging-app.retina.fm",
  },
  prod: {
    api:       "https://api.retina.fm",
    map:       "https://app.retina.fm",
    testmap:   null,
    dash:      "https://app.retina.fm",
    // Null on prod, like testmap and for the same reason: a failed production
    // E2E rolls production back, and the surface selection this would assert is
    // client-side, so staging exercises the identical bundle at no such cost.
    admin:     null,
    // Null for that same reason. What a browser would add over the production
    // smoke tests is that the bundle executes, and those already assert it
    // directly: check_page_asset follows the page's own script URL and requires
    // JavaScript back (deploy/page-asset.sh).
    app:       null,
  },
  local: {
    api:       "http://localhost:8000",
    // The console's dev server, map included.
    map:       "http://localhost:5174",
    testmap:   "http://localhost:5174",
    dash:      "http://localhost:5174",
    // Null for a different reason than prod: the dev server answers on one
    // origin, so no hostname there selects the admin surface. Every value here
    // is a bare origin that call sites append paths to, and `?mode=admin` is a
    // query rather than an origin, so it cannot live in this table.
    admin:     null,
    // Null because the redirects that suite asserts are nginx's, not Vite's.
    app:       null,
  },
} as const;

export const env = ENV;
export const hosts = HOSTS[ENV];

/**
 * Cloudflare Access service-token headers, empty unless CI supplies both.
 *
 * The admin vhost sits behind an Access application, which answers a request
 * carrying no session with a 302 to its login page. Playwright follows that
 * redirect and lands on Cloudflare's HTML, so without these a test against the
 * admin surface fails somewhere unhelpful — parsing a login page as the app —
 * rather than saying it was never let in.
 *
 * Empty on an ungated hostname and for anyone running the suite locally, so
 * this changes nothing until the Access applications and the token both exist.
 * Both halves or neither: half a credential is refused at the edge exactly like
 * none, and sending one would only make the failure harder to read.
 *
 * Exported because `request.newContext()` does not inherit `use`, so a spec
 * building its own context against a gated host has to pass these itself.
 */
export const accessHeaders: Record<string, string> =
  process.env.CF_ACCESS_CLIENT_ID && process.env.CF_ACCESS_CLIENT_SECRET
    ? {
        "CF-Access-Client-Id": process.env.CF_ACCESS_CLIENT_ID,
        "CF-Access-Client-Secret": process.env.CF_ACCESS_CLIENT_SECRET,
      }
    : {};

export default defineConfig({
  testDir: "./specs",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    // The console, which exists on every environment and is ours: testmap is
    // null wherever no fleet runs and the towers name is not ours. Every spec
    // names its host explicitly, so this only resolves a relative URL.
    baseURL: hosts.map,
    extraHTTPHeaders: accessHeaders,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
