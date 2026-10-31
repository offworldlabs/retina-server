import { defineConfig, devices, test } from "@playwright/test";

/**
 * Playwright E2E test configuration.
 *
 * Environments (set via E2E_ENV):
 *   staging  → staging-api / staging-app / staging-admin (default)
 *   prod     → api / app (no synthetic map, no admin)
 *   local    → localhost:8000 (api) / localhost:5174 (the console, map included)
 *              / admin.localhost:5174, or the console on E2E_LOCAL_PORT when a
 *              second dev server runs beside the first
 *
 * E2E_ENV=local also adds the two desktop projects, which compare every page
 * of the console with screenshots kept on this machine (see
 * specs/desktop-unchanged.spec.ts), and the eight phone and tablet projects,
 * which check every page against the running stack (see
 * specs/mobile-layout.spec.ts). They exist nowhere else, so no deployed suite
 * lists them.
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
 * `testmap` names the origin whose admin console has a simulator at /sim. It
 * is null on prod AND on staging, and that is load-bearing rather than
 * tidiness: neither runs a simulator (only the test droplet does), so neither
 * has a /sim at all. Pointing a deployed suite at another environment's
 * simulator would mean that suite exercising a box it does not deploy, and
 * because a failed production E2E auto-rolls-back production
 * (production-deploy-verify.yml), a wobble elsewhere would revert a good
 * production build. The one suite that needs the surface skips itself
 * instead, and runs locally against the dev server's admin console.
 */

// The dev server's port. A worktree that runs its own stack beside another
// one serves the console on a port of its own.
const LOCAL_PORT = process.env.E2E_LOCAL_PORT || "5174";
if (!/^\d{1,5}$/.test(LOCAL_PORT)) throw new Error(`E2E_LOCAL_PORT=${LOCAL_PORT} is not a port number`);

const HOSTS = {
  staging: {
    api:       "https://staging-api.retina.fm",
    // The consolidated surface, whose root opens on the live map.
    map:       "https://staging-app.retina.fm",
    // The origin whose admin console has a /sim page, which is what the
    // live-map suite needs. Staging runs no fleet (its `fleet` service sits
    // behind the same unenabled `sim` profile as production's), so the suite
    // skips here as it does on prod.
    testmap:   null,
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
    map:       `http://localhost:${LOCAL_PORT}`,
    // The dev server answers on every `*.localhost` name, and this one selects
    // the admin console, which holds /sim.
    testmap:   `http://admin.localhost:${LOCAL_PORT}`,
    dash:      `http://localhost:${LOCAL_PORT}`,
    // Chromium resolves it itself; Node on macOS does not, so the admin-surface
    // tests, which check it from Node first, skip there.
    admin:     `http://admin.localhost:${LOCAL_PORT}`,
    // Null because the redirects that suite asserts are nginx's, not Vite's.
    app:       null,
  },
} as const;

// e2e's collect script lists these environments by name; a new one goes there too.
const isEnv = (name: string): name is keyof typeof HOSTS => Object.keys(HOSTS).includes(name);
const ENV = process.env.E2E_ENV ?? "staging";
if (!isEnv(ENV)) throw new Error(`E2E_ENV=${ENV} is none of ${Object.keys(HOSTS).join(", ")}`);
const TABLE = HOSTS[ENV];

export const env = ENV;

/**
 * The Chromium the desktop projects and the HAR recorder launch, from
 * E2E_CHROMIUM_PATH; undefined (Playwright's own build) when unset. A machine
 * that lacks Playwright's own build points it at a fixed Chromium build, never
 * at an installed Chrome, which updates itself and would move pixels between
 * runs. Only a local run reads it, so collecting a deployed suite is never
 * affected by it.
 */
export const chromiumExecutable: string | undefined = (() => {
  const wanted = process.env.E2E_CHROMIUM_PATH || undefined;
  if (ENV !== "local" || wanted === undefined) return undefined;
  const fs = (process as typeof process & {
    getBuiltinModule(id: "node:fs"): { statSync(path: string, options: { throwIfNoEntry: false }): { isFile(): boolean } | undefined };
  }).getBuiltinModule("node:fs");
  if (!fs.statSync(wanted, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`E2E_CHROMIUM_PATH=${wanted} is not an existing file`);
  }
  return wanted;
})();

/**
 * How the desktop projects and the HAR recorder launch Chromium. Repaints of
 * part of a tile leave anti-aliasing on card edges that depends on the order
 * of the repaints while a page settles; rasterising each tile in full makes
 * two runs of the same page identical.
 */
export const desktopLaunchOptions = {
  executablePath: chromiumExecutable,
  args: ["--disable-partial-raster"],
};

// The roles every environment has. The rest are reached through hostOrSkip.
export const hosts: Record<"api" | "map" | "dash", string> = {
  api: TABLE.api,
  map: TABLE.map,
  dash: TABLE.dash,
};

/**
 * The host for a role that is null on some environment, skipping where it is:
 * the file at a spec's top level, the group in a describe or beforeAll, the test
 * in a test or beforeEach. Call it from the spec itself: a shared module runs
 * once, so only the first spec to import it would skip. Where it skips it
 * returns an unroutable stand-in, so top-level code that parses it cannot throw.
 */
export function hostOrSkip(role: Exclude<keyof typeof TABLE, keyof typeof hosts>, reason: string): string {
  const host = TABLE[role];
  test.skip(host === null, reason);
  return host ?? `https://${role}.skipped.invalid`;
}

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

/** The desktop sizes every page is compared at, one project each. */
export const DESKTOP_VIEWPORTS = [
  { name: "desktop-1440", width: 1440, height: 900 },
  { name: "desktop-1024", width: 1024, height: 768 },
] as const;

// Matched by path, so the suite that gates deploys never loads it.
const DESKTOP_SPEC = "**/desktop-unchanged.spec.ts";

// The screenshots belong to the machine that took them (fonts, rasteriser),
// so they live outside the repository. Unset, the projects still collect, but
// every test skips; the fallback is a gitignored folder so a stray update
// cannot land anywhere tracked.
const PIXEL_DIR = process.env.E2E_PIXEL_DIR || "screenshots";

const desktopProjects = DESKTOP_VIEWPORTS.map(({ name, width, height }) => ({
  name,
  testMatch: DESKTOP_SPEC,
  snapshotPathTemplate: `${PIXEL_DIR}/{projectName}/{arg}{ext}`,
  expect: {
    // Exact: the point is to prove the desktop did not move by one pixel.
    toHaveScreenshot: {
      maxDiffPixels: 0,
      threshold: 0,
      animations: "disabled" as const,
      caret: "hide" as const,
      scale: "css" as const,
    },
  },
  // Playwright's own Chromium rather than the installed Chrome the deployed
  // suites drive: a Chrome update must not move a pixel between two runs.
  // E2E_CHROMIUM_PATH names a fixed build where Playwright's own is missing.
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width, height },
    deviceScaleFactor: 1,
    launchOptions: desktopLaunchOptions,
  },
}));

// Matched by path like the desktop spec, and for the same reason.
const TOUCH_SPEC = "**/mobile-layout.spec.ts";

// The Chromium the desktop projects launch, or the installed Chrome where
// E2E_CHROMIUM_PATH names no build: these projects compare no pixels, so a
// Chrome update cannot fail them.
const touchChromium = chromiumExecutable ? { launchOptions: { executablePath: chromiumExecutable } } : { channel: "chrome" };

/** A phone or tablet in Chromium, which stands for Android. It claims the
 *  agent of the WebKit project at its size, so a page that read the agent
 *  would draw the same in both engines. */
const chromiumTouch = (name: string, width: number, height: number, deviceScaleFactor: number, agentOf: "iPhone 15" | "iPad Mini") => ({
  name,
  testMatch: TOUCH_SPEC,
  use: {
    browserName: "chromium" as const,
    ...touchChromium,
    userAgent: devices[agentOf].userAgent,
    viewport: { width, height },
    screen: { width, height },
    deviceScaleFactor,
    isMobile: true,
    hasTouch: true,
  },
});

/** A phone or tablet in WebKit, the engine of every browser on iOS, from
 *  Playwright's device list, at the given size where it differs. */
const webkitTouch = (name: string, device: "iPhone 15" | "iPad Mini" | "iPad Pro 11", size?: { width: number; height: number }) => ({
  name,
  testMatch: TOUCH_SPEC,
  use: { ...devices[device], ...(size ? { viewport: size, screen: size } : {}) },
});

/** The phone and tablet projects: a phone at 390 and 360 px, a tablet at
 *  768 px, where the sidebar is still a drawer, and at 820 px, where it is
 *  the rail. */
const touchProjects = [
  chromiumTouch("phone", 390, 844, 3, "iPhone 15"),
  chromiumTouch("phone-small", 360, 800, 3, "iPhone 15"),
  webkitTouch("phone-webkit", "iPhone 15", { width: 390, height: 844 }),
  webkitTouch("phone-small-webkit", "iPhone 15", { width: 360, height: 800 }),
  chromiumTouch("tablet", 768, 1024, 2, "iPad Mini"),
  chromiumTouch("tablet-wide", 820, 1180, 2, "iPad Mini"),
  webkitTouch("tablet-webkit", "iPad Mini"),
  webkitTouch("tablet-wide-webkit", "iPad Pro 11", { width: 820, height: 1180 }),
];

export default defineConfig({
  testDir: "./specs",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  // A committed test.only would quietly shrink the suite that decides whether
  // production rolls back, so in CI it fails the run instead.
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    // The console, which exists on every environment and is ours: testmap is
    // null wherever no fleet runs and the towers name is not ours. Every spec
    // names its host explicitly, so this only resolves a relative URL.
    baseURL: hosts.map,
    extraHTTPHeaders: accessHeaders,
    // Never in CI, whose artifacts are public: a trace records every request's
    // headers, keys included.
    trace: process.env.CI ? "off" : "on-first-retry",
    screenshot: "only-on-failure",
    headless: true,
  },
  projects: [
    {
      name: "chromium",
      testIgnore: [DESKTOP_SPEC, TOUCH_SPEC],
      // Google Chrome as installed rather than a browser Playwright downloads:
      // CI uses the one its runner image ships, and needs no install step.
      use: { ...devices["Desktop Chrome"], channel: "chrome" },
    },
    ...(ENV === "local" ? [...desktopProjects, ...touchProjects] : []),
  ],
});
