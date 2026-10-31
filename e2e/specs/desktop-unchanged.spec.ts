/**
 * The console's desktop look, page by page, at 1440×900 and 1024×768.
 *
 * Each test opens one page against the API answers scripts/record-har.ts
 * recorded for its role, staged, settled and scrolled exactly as they were
 * recorded (fixtures/routes.ts), and compares the viewport, then each further
 * screen of the scrolling pane, pixel for pixel with the screenshots in
 * E2E_PIXEL_DIR. Nothing reaches the backend, a socket or the internet, so a
 * difference is the console's own.
 *
 * The two projects exist only under E2E_ENV=local, against the dev server on
 * E2E_LOCAL_PORT, and are never part of a deployed suite. Every test skips
 * without E2E_PIXEL_DIR, and a page whose path needs a value E2E_STATE lacks
 * skips; with E2E_PIXEL_DIR set, a missing recording fails.
 *
 * From e2e/, with the recordings in E2E_HAR_DIR:
 *
 *   E2E_ENV=local E2E_LOCAL_PORT=… E2E_STATE=… E2E_HAR_DIR=… E2E_PIXEL_DIR=… \
 *     npx playwright test --project desktop-1440 --project desktop-1024 --update-snapshots=all
 *
 * takes the screenshots, and the same with --update-snapshots=none compares
 * with them, failing on a screen the page has gained as well as on one that
 * changed or went.
 */
import { expect, test, type TestInfo } from "@playwright/test";
import {
  FIRST_SETTLE_MS,
  MAX_SCREENS,
  PAGE_CONTEXT,
  ROUTES,
  hideUnstableLayers,
  insideRepository,
  loadSeedState,
  localSites,
  missingValues,
  nodeFs,
  nodePath,
  recordingFiles,
  resolvePath,
  screenName,
  settlePage,
  siteOrigin,
  stagePage,
  walkScreens,
  type PageRoute,
  type PageWatch,
  type Recording,
} from "../fixtures/routes";

test.use({
  locale: PAGE_CONTEXT.locale,
  timezoneId: PAGE_CONTEXT.timezoneId,
  colorScheme: PAGE_CONTEXT.colorScheme,
  serviceWorkers: PAGE_CONTEXT.serviceWorkers,
  contextOptions: { reducedMotion: PAGE_CONTEXT.reducedMotion },
});

// A slow first settle and six screens, each settled after its scroll.
const TEST_TIMEOUT_MS = 180_000;

/** The recording `route` replays from, read and checked against this run. */
function recordingFor(route: PageRoute, testInfo: TestInfo): { har: string; t0: number } {
  const harDir = process.env.E2E_HAR_DIR;
  if (!harDir) throw new Error("E2E_HAR_DIR is not set: it names the folder scripts/record-har.ts recorded into");
  const files = recordingFiles(nodePath().resolve(harDir), route.role);
  for (const file of [files.har, files.sidecar]) {
    if (!nodeFs().existsSync(file)) {
      throw new Error(`${file} is missing: record the ${route.role} pages with node e2e/scripts/record-har.ts ${route.role}`);
    }
  }
  const recording = JSON.parse(nodeFs().readFileSync(files.sidecar, "utf8")) as Partial<Recording>;
  const t0 = Date.parse(recording.t0 ?? "");
  if (!Number.isFinite(t0)) throw new Error(`${files.sidecar} names no instant (t0) to hold the clock at`);
  // The HAR answers by URL, origin included: replayed on another port, every
  // request would miss.
  const sites = localSites();
  if (recording.origin?.app !== sites.app || recording.origin?.admin !== sites.admin) {
    throw new Error(
      `${files.har} was recorded against ${recording.origin?.app ?? "an unknown origin"}, ` +
        `and this run serves ${sites.app}: set E2E_LOCAL_PORT to the recording's port`,
    );
  }
  if (!recording.routes?.includes(route.id)) {
    throw new Error(`${files.har} holds no visit to ${route.id}: record the ${route.role} pages again with this E2E_STATE`);
  }
  const unsettled = recording.unsettled?.find(({ id, viewport }) => id === route.id && viewport === testInfo.project.name);
  if (unsettled) testInfo.annotations.push({ type: "recorded before it settled", description: unsettled.reason });
  return { har: files.har, t0 };
}

/** Lists in the test's output what the page did that the replay withheld. */
function listWithheld(route: PageRoute, testInfo: TestInfo, watch: PageWatch): void {
  const lists: [string, string[]][] = [
    ["aborted off-origin request", watch.offsite],
    ["failed API request", watch.failedApi],
    ["page error", watch.errors],
  ];
  const lines: string[] = [];
  for (const [type, list] of lists) {
    for (const description of list) testInfo.annotations.push({ type, description });
    if (list.length > 0) lines.push(`${type}s:\n    ${list.join("\n    ")}`);
  }
  if (lines.length > 0) console.log(`${route.id} at ${testInfo.project.name}:\n  ${lines.join("\n  ")}`);
}

/** Fails on a screenshot past the page's last screen, which says the page got
 *  shorter; taking the screenshots again deletes it instead. */
function checkNoScreensBeyond(route: PageRoute, testInfo: TestInfo, screens: number): void {
  const updating = testInfo.config.updateSnapshots === "all" || testInfo.config.updateSnapshots === "changed";
  for (let index = screens + 1; index <= MAX_SCREENS; index++) {
    const file = testInfo.snapshotPath(screenName(route.id, index), { kind: "screenshot" });
    const stale = nodeFs().existsSync(file);
    if (stale && updating) nodeFs().rmSync(file, { recursive: true, force: true });
    else expect.soft(stale, `${route.id} has ${screens} screens now, so ${file} shows one it no longer has`).toBe(false);
  }
}

// Decided when the file is collected, so a skipped test sets up no browser.
// The list stays the same either way: a skipped test is still listed.
const PIXEL_DIR = process.env.E2E_PIXEL_DIR;
test.skip(!PIXEL_DIR, "E2E_PIXEL_DIR is not set, so there are no screenshots to compare with");
const STATE = PIXEL_DIR ? loadSeedState() : null;

for (const route of ROUTES) {
  const path = resolvePath(route, STATE);
  const missing = missingValues(route, STATE);
  const skipped = Boolean(PIXEL_DIR) && path === null;
  const reason = { type: "missing seeded value", description: `the page's path needs ${missing.join(", ")} from E2E_STATE` };
  (skipped ? test.skip : test)(`${route.id} looks the same`, { annotation: skipped ? [reason] : [] }, async ({ page }, testInfo) => {
    test.setTimeout(TEST_TIMEOUT_MS);
    // Never true here, given the skips above; it narrows the types.
    if (!PIXEL_DIR || path === null) return;
    if (!nodePath().isAbsolute(PIXEL_DIR) || insideRepository(PIXEL_DIR)) {
      throw new Error(`E2E_PIXEL_DIR=${PIXEL_DIR} must be an absolute path outside the repository`);
    }
    const { har, t0 } = recordingFor(route, testInfo);

    const watch = await stagePage(page, { t0, har });
    try {
      await page.goto(`${siteOrigin(route.site)}${path}`, { waitUntil: "load", timeout: FIRST_SETTLE_MS });
      await settlePage(page, watch, FIRST_SETTLE_MS);
      await hideUnstableLayers(page);
      const screens = await walkScreens(page, watch, async (index) => {
        await expect.soft(page, `screen ${index} of ${route.id}`).toHaveScreenshot(screenName(route.id, index));
      });
      checkNoScreensBeyond(route, testInfo, screens);
    } finally {
      listWithheld(route, testInfo, watch);
    }
  });
}
