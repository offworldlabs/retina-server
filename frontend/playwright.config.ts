import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E test configuration.
 *
 * Environments (set via E2E_ENV):
 *   staging  → staging-towers.retina.fm / staging-api.retina.fm / staging-map.retina.fm (default)
 *   prod     → towers.retina.fm / api.retina.fm / (no synthetic map)
 *   local    → localhost:5173 / localhost:8000
 *
 * `testmap` is null on prod, and that is load-bearing rather than tidiness.
 * testmap.retina.fm is served by staging — production runs no simulator and has
 * no synthetic map surface. Pointing the production suite at it would mean the
 * production E2E exercising staging, and because a failed production E2E
 * auto-rolls-back production (ci.yml), a staging wobble would revert a good
 * production build. The one suite that needs the surface skips itself instead.
 */

const ENV = (process.env.E2E_ENV ?? "staging") as "staging" | "prod" | "local";

const HOSTS = {
  staging: {
    frontend:  "https://staging-towers.retina.fm",
    api:       "https://staging-api.retina.fm",
    map:       "https://staging-map.retina.fm",
    // The synthetic map surface, which is what the live-map suite needs — and
    // deliberately NOT testmap.retina.fm, even though staging now serves that
    // too. That name's record is Cloudflare-side and points at whichever box
    // currently hosts the demo, so keying CI to it would fail the suite for the
    // duration of any DNS move, including the one that first brings it here.
    // staging-map serves byte-identical data: usesRealOnlyFeed is anchored to
    // `^map\.` exactly, so neither host filters to the real-only feed.
    testmap:   "https://staging-map.retina.fm",
    dash:      "https://staging-dash.retina.fm",
    // Same bundle as dash; the hostname is what selects the admin route table.
    admin:     "https://staging-admin.retina.fm",
  },
  prod: {
    frontend:  "https://towers.retina.fm",
    api:       "https://api.retina.fm",
    map:       "https://map.retina.fm",
    testmap:   null,
    dash:      "https://dash.retina.fm",
    // Null on prod, like testmap and for the same reason: a failed production
    // E2E rolls production back, and the surface selection this would assert is
    // client-side, so staging exercises the identical bundle at no such cost.
    admin:     null,
  },
  local: {
    frontend:  "http://localhost:5173",
    api:       "http://localhost:8000",
    map:       "http://localhost:5173",
    testmap:   "http://localhost:5173",
    dash:      "http://localhost:5174",
    // Null for a different reason than prod: the dev server answers on one
    // origin, so no hostname there selects the admin surface. Every value here
    // is a bare origin that call sites append paths to, and `?mode=admin` is a
    // query rather than an origin, so it cannot live in this table.
    admin:     null,
  },
} as const;

export const env = ENV;
export const hosts = HOSTS[ENV];

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: hosts.frontend,
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
