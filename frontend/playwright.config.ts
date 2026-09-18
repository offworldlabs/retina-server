import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E test configuration.
 *
 * Environments (set via E2E_ENV):
 *   staging  → staging-api / staging-app / staging-admin (default)
 *   prod     → api / app (no synthetic map, no admin)
 *   local    → localhost:8000 (api) / localhost:5173 (map) / localhost:5174 (dash)
 *
 * The entries below are roles, not hostnames, which is why several of them hold
 * the same origin on a deployed environment: one hostname now serves the map at
 * `/` and the dashboard under `/dash/`. They stay separate because they differ
 * where it matters — on production and on the dev server, where each answers
 * its own question about what exists.
 *
 * No entry names a towers hostname. Those are routed to tower-finder-service's
 * own edge by a Cloudflare Origin Rule, so nothing this repo builds answers
 * there: a test against one asserts another service's markup, and on prod a
 * failed E2E rolls production back.
 *
 * `testmap` is null on prod, and that is load-bearing rather than tidiness.
 * Production runs no simulator and has no synthetic map surface; staging is the
 * only environment that still has one. Pointing the production suite at
 * staging's would mean the production E2E exercising staging, and because a
 * failed production E2E auto-rolls-back production (ci.yml), a staging wobble
 * would revert a good production build. The one suite that needs the surface
 * skips itself instead.
 */

const ENV = (process.env.E2E_ENV ?? "staging") as "staging" | "prod" | "local";

const HOSTS = {
  staging: {
    api:       "https://staging-api.retina.fm",
    // The live map, at the root of the consolidated surface.
    map:       "https://staging-app.retina.fm",
    // The synthetic map surface, which is what the live-map suite needs. Staging
    // is the environment running the fleet, so on staging it is the same origin
    // as `map` above: the two differ on production, where one exists and the
    // other does not.
    testmap:   "https://staging-app.retina.fm",
    // The dashboard's origin. Its pages sit under dashBase below; its
    // same-origin API calls do not.
    dash:      "https://staging-app.retina.fm",
    // Same bundle as dash, built at a root instead; the hostname is what selects
    // the admin route table. It keeps a name of its own because a Cloudflare
    // Access application can only be scoped to one.
    admin:     "https://staging-admin.retina.fm",
    // The consolidated surface as a whole, for the suite that asserts the mounts
    // themselves execute rather than any one bundle's behaviour.
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
    // smoke tests is that the mounted bundles execute, and those already assert
    // it directly: check_page_asset follows each page's own script URL and
    // requires JavaScript back (deploy/page-asset.sh).
    app:       null,
  },
  local: {
    api:       "http://localhost:8000",
    map:       "http://localhost:5173",
    testmap:   "http://localhost:5173",
    dash:      "http://localhost:5174",
    // Null for a different reason than prod: the dev server answers on one
    // origin, so no hostname there selects the admin surface. Every value here
    // is a bare origin that call sites append paths to, and `?mode=admin` is a
    // query rather than an origin, so it cannot live in this table.
    admin:     null,
    // Null because the mounts are nginx's, not Vite's: each dev server serves
    // one bundle at its own root, so there is no /dash/ to visit locally.
    app:       null,
  },
} as const;

export const env = ENV;
export const hosts = HOSTS[ENV];

/**
 * The path the dashboard bundle is mounted at on `hosts.dash`.
 *
 * Empty on the dev server, which gives the bundle an origin to itself, and
 * `/dash` on a deployed environment, where it shares one with the map. A page
 * route takes this prefix and a same-origin API call does not — the API is the
 * vhost's, not the bundle's.
 */
export const dashBase = ENV === "local" ? "" : "/dash";

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
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    // The frontend/dist surface that exists on every environment and is ours:
    // testmap is staging-only and the towers name is not ours. Every spec names
    // its host explicitly, so this only resolves a relative URL.
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
