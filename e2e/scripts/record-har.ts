/**
 * Records the API answers the desktop screenshots replay
 * (specs/desktop-unchanged.spec.ts), one role per run.
 *
 * Each page of the role is opened in a fresh context at every desktop size,
 * staged exactly as the replay stages it (fixtures/routes.ts: the clock held
 * at the recording's start, pinned storage, silent sockets, fixed tiles) and
 * settled and scrolled the same way, so every request the replay will make is
 * made here first. Only the dev server's /api/ traffic is kept, bodies
 * embedded and cookies stripped, in <E2E_HAR_DIR>/<role>.har, beside
 * <role>.json, which holds the instant the clock was held at (the replay
 * holds it there too), the origins, the pages and the Playwright version.
 *
 * Before it records, it asks /api/auth/me on each console the role's pages are
 * on, and stops unless the answer is that role's, since the backend's mode is
 * what decides it:
 *
 *   owner, signedout   the backend enforcing sign-in. The owner comes from
 *                      E2E_OWNER_STATE when it holds a live session, otherwise
 *                      from a mailed link (fixtures/ownerSession.ts), which
 *                      needs MAIL_TRANSPORT=log and E2E_MAIL_LOG.
 *   admin              the backend with AUTH_ALLOW_ANONYMOUS_ADMIN=1, and the
 *                      synthetic fleet on for the simulator's two pages.
 *
 * Run it with the dev server up, from anywhere, by its path; Node 22.18 and
 * later strip the types themselves (22.6 to 22.17 need
 * --experimental-strip-types). E2E_HAR_DIR must lie outside the repository.
 *
 *   E2E_ENV=local E2E_LOCAL_PORT=5174 \
 *   E2E_STATE=/abs/seed-state.json E2E_HAR_DIR=/abs/har \
 *   E2E_OWNER_STATE=/abs/auth/owner.json E2E_MAIL_LOG=/abs/backend.log \
 *     node e2e/scripts/record-har.ts owner
 *
 * The role is the argument, or E2E_ROLE. It exits 0 when every page settled,
 * 1 when one did not (the HAR is still written, and <role>.json names the
 * page) or the backend answered for another role, and 2 on a setup error.
 */
import { chromium, devices } from "@playwright/test";
import type { Browser } from "@playwright/test";
import { DESKTOP_VIEWPORTS, chromiumExecutable, desktopLaunchOptions, env } from "../playwright.config.ts";
import { signInOwner, type OwnerSession } from "../fixtures/ownerSession.ts";
import {
  FIRST_SETTLE_MS,
  PAGE_CONTEXT,
  ROUTES,
  insideRepository,
  loadSeedState,
  localSites,
  missingValues,
  nodeFs,
  nodePath,
  nodeProcess,
  recordingFiles,
  resolvePath,
  settlePage,
  siteOrigin,
  stagePage,
  walkScreens,
  type PageRoute,
  type PageWatch,
  type Recording,
  type Role,
  type SeedState,
  type Site,
} from "../fixtures/routes.ts";

const ROLES: readonly Role[] = ["owner", "signedout", "admin"];

/** A run that cannot start as set up: exit 2. */
class SetupError extends Error {}

type StorageState = OwnerSession["storageState"];

interface HarHeader {
  name: string;
  value: string;
}

interface HarEntry {
  request: { headers: HarHeader[]; cookies?: unknown[] };
  response: { headers: HarHeader[]; cookies?: unknown[] };
}

interface HarFile {
  log: { entries: HarEntry[] } & Record<string, unknown>;
}

const escapeRegExp = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** The dev server's own API, on either console. */
function apiTraffic(): RegExp {
  const { app, admin } = localSites();
  return new RegExp(`^(?:${escapeRegExp(app)}|${escapeRegExp(admin)})/api/`);
}

/** Sent by the browser, and a session's secret. */
const SECRET_REQUEST_HEADERS = /^(?:cookie|authorization)$/i;
/** The session's secret, and an encoding the recorded body no longer has:
 *  Playwright keeps the decoded bytes. */
const DROPPED_RESPONSE_HEADERS = /^(?:set-cookie|content-encoding)$/i;

const errorText = (error: unknown) => (error instanceof Error ? error.message : String(error)).split("\n")[0];

/** Why /api/auth/me's answer is not `role`'s, or null when it is. */
function wrongRole(role: Role, status: number, body: { role?: string; auth_enabled?: boolean } | null): string | null {
  const got = `/api/auth/me answered ${status}${body?.role ? ` as ${body.role}` : ""}`;
  if (role === "signedout") return status === 401 ? null : `${got}, not 401: record it with the backend enforcing sign-in`;
  if (role === "admin") {
    return status === 200 && body?.role === "admin" ? null : `${got}, not an administrator: record it with AUTH_ALLOW_ANONYMOUS_ADMIN=1`;
  }
  if (status !== 200 || body?.role === "admin" || body?.auth_enabled === false) {
    return `${got}, not the owner: record it with the backend enforcing sign-in`;
  }
  return null;
}

/** Stops unless /api/auth/me, asked by a page on each of `sites`, is `role`'s. */
async function expectRole(browser: Browser, role: Role, sites: Site[], storageState?: StorageState): Promise<void> {
  const context = await browser.newContext({ storageState });
  try {
    const page = await context.newPage();
    for (const site of sites) {
      const response = await page.goto(`${siteOrigin(site)}/api/auth/me`);
      const status = response?.status() ?? 0;
      const body = status === 200 && response ? await response.json().catch(() => null) : null;
      const wrong = wrongRole(role, status, body);
      if (wrong) throw new Error(`${role} on the ${site} console: ${wrong}`);
    }
  } finally {
    await context.close();
  }
}

/** The owner's session, from E2E_OWNER_STATE or a mailed link. */
async function ownerState(browser: Browser): Promise<StorageState> {
  const context = await browser.newContext();
  try {
    return (await signInOwner(context, siteOrigin("app"))).storageState;
  } catch (error) {
    throw new SetupError(errorText(error));
  } finally {
    await context.close();
  }
}

function scrub(entry: HarEntry): HarEntry {
  entry.request.headers = entry.request.headers.filter((header) => !SECRET_REQUEST_HEADERS.test(header.name));
  entry.response.headers = entry.response.headers.filter((header) => !DROPPED_RESPONSE_HEADERS.test(header.name));
  entry.request.cookies = [];
  entry.response.cookies = [];
  return entry;
}

/** The entries of every part, in order, under the first part's header. */
function mergeHars(parts: string[]): HarFile {
  const fs = nodeFs();
  let merged: HarFile | null = null;
  for (const part of parts) {
    // A context that crashed writes nothing.
    if (!fs.existsSync(part)) continue;
    const har = JSON.parse(fs.readFileSync(part, "utf8")) as HarFile;
    merged ??= { log: { ...har.log, entries: [] } };
    for (const entry of har.log.entries) merged.log.entries.push(scrub(entry));
  }
  return merged ?? { log: { version: "1.2", creator: { name: "Playwright" }, entries: [] } };
}

/** Writes `text` to `file` whole or not at all. */
function writeAtomically(file: string, text: string): void {
  const fs = nodeFs();
  fs.writeFileSync(`${file}.partial`, text);
  fs.renameSync(`${file}.partial`, file);
}

function playwrightVersion(): string {
  const file = nodeProcess.getBuiltinModule("node:url").fileURLToPath(new URL(import.meta.resolve("@playwright/test/package.json")));
  return (JSON.parse(nodeFs().readFileSync(file, "utf8")) as { version: string }).version;
}

/** What a page did that the replay will have to do without. */
function report(label: string, watch: PageWatch): void {
  const lists: [string, string[]][] = [
    ["aborted off-origin", watch.offsite],
    ["failed API requests", watch.failedApi],
    ["page errors", watch.errors],
  ];
  for (const [what, list] of lists) {
    if (list.length > 0) console.log(`  ${label}: ${what}:\n    ${list.join("\n    ")}`);
  }
}

async function main(): Promise<number> {
  const role = (nodeProcess.argv[2] || process.env.E2E_ROLE || "") as Role;
  if (!ROLES.includes(role)) throw new SetupError(`name the role to record, ${ROLES.join(", ")}: as the argument or E2E_ROLE`);
  if (env !== "local") throw new SetupError("it records only from the local stack: set E2E_ENV=local");
  const harSetting = process.env.E2E_HAR_DIR;
  if (!harSetting) throw new SetupError("set E2E_HAR_DIR to a folder outside the repository");
  const harDir = nodePath().resolve(harSetting);
  if (insideRepository(harDir)) throw new SetupError(`E2E_HAR_DIR=${harDir} is inside the repository, which is public`);

  let state: SeedState | null;
  try {
    state = loadSeedState();
  } catch (error) {
    throw new SetupError(errorText(error));
  }
  const visits: { route: PageRoute; path: string }[] = [];
  const skipped: Recording["skipped"] = [];
  for (const route of ROUTES.filter((candidate) => candidate.role === role)) {
    const path = resolvePath(route, state);
    if (path === null) skipped.push({ id: route.id, missing: missingValues(route, state) });
    else visits.push({ route, path });
  }
  for (const { id, missing } of skipped) console.log(`skipped ${id}: E2E_STATE has no ${missing.join(", ")}`);
  if (visits.length === 0) throw new SetupError(`no ${role} page can be opened with this E2E_STATE`);

  const fs = nodeFs();
  const path = nodePath();
  fs.mkdirSync(harDir, { recursive: true });
  const files = recordingFiles(harDir, role);
  const partsDir = path.join(harDir, `.${role}-parts`);
  fs.rmSync(partsDir, { recursive: true, force: true });
  fs.mkdirSync(partsDir, { recursive: true });

  // Whole seconds, so the instant reads cleanly in <role>.json.
  const t0 = Math.floor(Date.now() / 1000) * 1000;
  const browser = await chromium.launch(desktopLaunchOptions);
  console.log(`browser: ${browser.version()}${chromiumExecutable ? " (E2E_CHROMIUM_PATH)" : ""}`);
  try {
    const storageState = role === "owner" ? await ownerState(browser) : undefined;
    const sites = Array.from(new Set(visits.map(({ route }) => route.site)));
    await expectRole(browser, role, sites, storageState);

    const parts: string[] = [];
    const unsettled: Recording["unsettled"] = [];
    for (const { route, path: routePath } of visits) {
      for (const { name, width, height } of DESKTOP_VIEWPORTS) {
        const part = path.join(partsDir, `${String(parts.length + 1).padStart(3, "0")}.har`);
        parts.push(part);
        const context = await browser.newContext({
          ...devices["Desktop Chrome"],
          viewport: { width, height },
          deviceScaleFactor: 1,
          locale: PAGE_CONTEXT.locale,
          timezoneId: PAGE_CONTEXT.timezoneId,
          colorScheme: PAGE_CONTEXT.colorScheme,
          reducedMotion: PAGE_CONTEXT.reducedMotion,
          serviceWorkers: PAGE_CONTEXT.serviceWorkers,
          storageState,
          recordHar: { path: part, urlFilter: apiTraffic(), content: "embed", mode: "minimal" },
        });
        const label = `${route.id} at ${name}`;
        try {
          const page = await context.newPage();
          const watch = await stagePage(page, { t0 });
          try {
            await page.goto(`${siteOrigin(route.site)}${routePath}`, { waitUntil: "load", timeout: FIRST_SETTLE_MS });
            await settlePage(page, watch, FIRST_SETTLE_MS);
            const screens = await walkScreens(page, watch, async () => {});
            console.log(`recorded ${label}: ${screens} screen${screens === 1 ? "" : "s"}`);
          } catch (error) {
            unsettled.push({ id: route.id, viewport: name, reason: errorText(error) });
            console.log(`NOT SETTLED ${label}: ${errorText(error)}`);
          }
          report(label, watch);
        } finally {
          // Closing the context is what writes its part.
          await context.close();
        }
      }
    }

    const har = mergeHars(parts);
    writeAtomically(files.har, JSON.stringify(har));
    const recording: Recording = {
      role,
      t0: new Date(t0).toISOString(),
      origin: localSites(),
      routes: visits.map(({ route }) => route.id),
      skipped,
      unsettled,
      viewports: DESKTOP_VIEWPORTS.map(({ name }) => name),
      playwright: playwrightVersion(),
      browser: browser.version(),
    };
    writeAtomically(files.sidecar, `${JSON.stringify(recording, null, 2)}\n`);
    fs.rmSync(partsDir, { recursive: true, force: true });
    console.log(
      `${role}: ${har.log.entries.length} API answers from ${visits.length} pages into ${files.har}` +
        (unsettled.length ? `; ${unsettled.length} visits did not settle` : ""),
    );
    return unsettled.length > 0 ? 1 : 0;
  } finally {
    await browser.close();
  }
}

main().then(
  (code) => {
    nodeProcess.exitCode = code;
  },
  (error: unknown) => {
    console.error(`record-har: ${errorText(error)}`);
    nodeProcess.exitCode = error instanceof SetupError ? 2 : 1;
  },
);
