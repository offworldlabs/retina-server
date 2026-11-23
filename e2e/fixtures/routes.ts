/**
 * The console pages the desktop screenshots cover, and how each visit is
 * staged so that a page draws the same pixels on every run.
 *
 * specs/desktop-unchanged.spec.ts replays recorded API answers through this
 * module and scripts/record-har.ts records them through it, so the recording
 * makes exactly the requests the replay will: same clock, same storage, same
 * sockets, same tiles, same settling, same scrolling.
 *
 * Relative imports carry their .ts extension because Node runs this module
 * directly for the recorder, stripping the types itself; Playwright resolves
 * them the same way.
 */
import type { Page, Request } from "@playwright/test";
import { hosts } from "../playwright.config.ts";

// ── Node built-ins ──────────────────────────────────────────────────────────
// The e2e program carries no Node types (see specs/env.d.ts), so the few
// built-ins the harness touches are typed here and fetched through
// process.getBuiltinModule rather than imported.

interface NodeFs {
  existsSync(path: string): boolean;
  readFileSync(path: string, encoding: "utf8"): string;
  writeFileSync(path: string, data: string): void;
  mkdirSync(path: string, options: { recursive: true }): void;
  statSync(path: string): { size: number };
  openSync(path: string, flags: "r"): number;
  readSync(fd: number, buffer: Uint8Array, offset: number, length: number, position: number): number;
  closeSync(fd: number): void;
  realpathSync(path: string): string;
  renameSync(from: string, to: string): void;
  rmSync(path: string, options: { recursive: true; force: true }): void;
  readdirSync(path: string): string[];
}

interface NodePath {
  resolve(...paths: string[]): string;
  join(...paths: string[]): string;
  dirname(path: string): string;
  basename(path: string): string;
  relative(from: string, to: string): string;
  isAbsolute(path: string): boolean;
  sep: string;
}

interface NodeRuntime {
  argv: string[];
  exitCode?: number;
  cwd(): string;
  getBuiltinModule(id: "node:fs"): NodeFs;
  getBuiltinModule(id: "node:path"): NodePath;
  getBuiltinModule(id: "node:url"): { fileURLToPath(url: URL): string };
  getBuiltinModule(id: "node:buffer"): { Buffer: { from(data: string, encoding: "base64"): Uint8Array } };
}

export const nodeProcess = process as typeof process & NodeRuntime;
export const nodeFs = (): NodeFs => nodeProcess.getBuiltinModule("node:fs");
export const nodePath = (): NodePath => nodeProcess.getBuiltinModule("node:path");

/** The checkout this file belongs to. */
export function repoRoot(): string {
  return nodeProcess.getBuiltinModule("node:url").fileURLToPath(new URL("../..", import.meta.url));
}

/** `target` resolved through symlinks as far as it exists, so a folder not
 *  created yet still compares against the repository's real path. */
function realPath(target: string): string {
  const fs = nodeFs();
  const path = nodePath();
  let head = path.resolve(target);
  const tail: string[] = [];
  while (!fs.existsSync(head)) {
    const parent = path.dirname(head);
    if (parent === head) break;
    tail.unshift(path.basename(head));
    head = parent;
  }
  return path.join(fs.existsSync(head) ? fs.realpathSync(head) : head, ...tail);
}

/** Whether `target` lies inside this checkout. Recordings, sessions and
 *  screenshots never may: the repository is public. */
export function insideRepository(target: string): boolean {
  const path = nodePath();
  const relative = path.relative(realPath(repoRoot()), realPath(target));
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

// ── The pages ───────────────────────────────────────────────────────────────

export type Site = "app" | "admin";
/** Who the page is opened as: the seeded owner, nobody, or the admin console's
 *  anonymous administrator. Each is recorded on its own, into its own file. */
export type Role = "owner" | "signedout" | "admin";

export interface PageRoute {
  /** Names the test and the screenshot files. */
  id: string;
  site: Site;
  /** May hold {PLACEHOLDERS} filled from the seeded state (see SEEDED). */
  path: string;
  role: Role;
}

/** A token no sign-in, claim or address-change link has, so those pages draw
 *  their refusal, which needs nothing seeded and no secret in the repository. */
const INVALID_TOKEN = "invalid0000000000000000000000000000000000";

export const ROUTES: readonly PageRoute[] = [
  { id: "app-root", site: "app", path: "/", role: "owner" },
  { id: "app-map", site: "app", path: "/map", role: "owner" },
  { id: "app-map-archive", site: "app", path: "/map#t=2026-10-18T20:00:00Z&span=5", role: "owner" },
  { id: "app-map-signedout", site: "app", path: "/map", role: "signedout" },
  { id: "app-overview", site: "app", path: "/overview", role: "owner" },
  { id: "app-node-detail", site: "app", path: "/nodes/{P1_REF}", role: "owner" },
  { id: "app-node-private", site: "app", path: "/nodes/{PRIV_REF}", role: "owner" },
  { id: "app-detections", site: "app", path: "/detections", role: "owner" },
  { id: "app-rf", site: "app", path: "/rf", role: "owner" },
  { id: "app-contribution", site: "app", path: "/contribution", role: "owner" },
  { id: "app-data", site: "app", path: "/data?from=2026-10-18&to=2026-10-18", role: "owner" },
  { id: "app-anomalies", site: "app", path: "/anomalies", role: "owner" },
  { id: "app-leaderboard", site: "app", path: "/leaderboard", role: "owner" },
  { id: "app-knowledge", site: "app", path: "/knowledge", role: "owner" },
  { id: "app-node-sites", site: "app", path: "/node-sites", role: "owner" },
  { id: "app-tunnel", site: "app", path: "/tunnel", role: "owner" },
  { id: "app-onboarding", site: "app", path: "/onboarding", role: "owner" },
  { id: "app-radar-new", site: "app", path: "/radars/new", role: "owner" },
  { id: "app-radar", site: "app", path: "/radars/{POLLED_REF}", role: "owner" },
  { id: "app-account", site: "app", path: "/account", role: "owner" },
  { id: "adm-health", site: "admin", path: "/", role: "admin" },
  { id: "adm-nodes", site: "admin", path: "/nodes", role: "admin" },
  { id: "adm-node-detail", site: "admin", path: "/nodes/{P1_REF}", role: "admin" },
  { id: "adm-fleet", site: "admin", path: "/fleet", role: "admin" },
  { id: "adm-analytics", site: "admin", path: "/analytics", role: "admin" },
  { id: "adm-mlat", site: "admin", path: "/mlat", role: "admin" },
  { id: "adm-sim", site: "admin", path: "/sim", role: "admin" },
  { id: "adm-physics", site: "admin", path: "/sim/physics", role: "admin" },
  { id: "adm-anomalies", site: "admin", path: "/anomalies", role: "admin" },
  { id: "adm-events", site: "admin", path: "/events", role: "admin" },
  { id: "adm-storage", site: "admin", path: "/storage", role: "admin" },
  { id: "adm-custody", site: "admin", path: "/custody", role: "admin" },
  { id: "adm-users", site: "admin", path: "/users", role: "admin" },
  { id: "adm-user-detail", site: "admin", path: "/users/{OWNER_ID}", role: "admin" },
  { id: "adm-config", site: "admin", path: "/config", role: "admin" },
  { id: "adm-system", site: "admin", path: "/system", role: "admin" },
  { id: "adm-infra", site: "admin", path: "/infrastructure", role: "admin" },
  { id: "adm-api-docs", site: "admin", path: "/api-docs", role: "admin" },
  { id: "auth-login-app", site: "app", path: "/login", role: "signedout" },
  { id: "auth-login-admin", site: "admin", path: "/login", role: "signedout" },
  { id: "auth-link-invalid", site: "app", path: `/auth/link/${INVALID_TOKEN}`, role: "signedout" },
  { id: "auth-claim-invalid", site: "app", path: `/auth/claim/${INVALID_TOKEN}`, role: "signedout" },
  { id: "auth-email-invalid", site: "app", path: `/auth/email/${INVALID_TOKEN}`, role: "signedout" },
];

/**
 * The dev server's two consoles, on E2E_LOCAL_PORT. The admin one is the app's
 * host under `admin.`, the name the dev server answers that selects the admin
 * console (dashboard/src/utils/surface.ts). Read when called, from the host
 * table E2E_ENV picks, which only E2E_ENV=local points at the dev server.
 */
export function localSites(): { app: string; admin: string } {
  const app = new URL(hosts.dash);
  return { app: app.origin, admin: `${app.protocol}//admin.${app.host}` };
}

/** The origin a site is served from on the dev server. */
export function siteOrigin(site: Site): string {
  return localSites()[site];
}

// ── The seeded state ────────────────────────────────────────────────────────

/** The file E2E_STATE names, as the local seeding writes it. Only the fields
 *  read here are typed. */
export interface SeedState {
  owner_email?: string;
  owner_id?: string | number;
  backend_log?: string;
  nodes?: Record<string, { node_id?: string; node_ref?: string } | undefined>;
}

/** Where each placeholder's value comes from in the seeded state. */
const SEEDED: Partial<Record<string, (state: SeedState) => unknown>> = {
  P1_REF: (state) => state.nodes?.P1?.node_ref,
  PRIV_REF: (state) => state.nodes?.PRIV?.node_ref,
  POLLED_REF: (state) => state.nodes?.POLLED?.node_ref,
  OWNER_ID: (state) => state.owner_id,
};

const PLACEHOLDER = /\{([A-Z0-9_]+)\}/g;

/** The placeholders a page's path holds, by name. */
export function placeholdersIn(route: PageRoute): string[] {
  const names: string[] = [];
  for (let match = PLACEHOLDER.exec(route.path); match; match = PLACEHOLDER.exec(route.path)) names.push(match[1]);
  PLACEHOLDER.lastIndex = 0;
  return names;
}

function seededValue(name: string, state: SeedState | null): string | null {
  const read = SEEDED[name];
  const value = read && state ? read(state) : undefined;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return typeof value === "string" && value !== "" ? value : null;
}

/** The placeholders of `route` the state has no value for. */
export function missingValues(route: PageRoute, state: SeedState | null): string[] {
  return placeholdersIn(route).filter((name) => seededValue(name, state) === null);
}

/** The page's path with its placeholders filled, or null when the state
 *  lacks one of them, which the caller reports as a skip. */
export function resolvePath(route: PageRoute, state: SeedState | null): string | null {
  if (missingValues(route, state).length > 0) return null;
  return route.path.replace(PLACEHOLDER, (_, name: string) => encodeURIComponent(seededValue(name, state) ?? ""));
}

/** The state E2E_STATE names, or null when it names none. A file it names but
 *  that cannot be read is an error, not a reason to skip: the run was set up
 *  wrong, and every skip would hide it. */
export function loadSeedState(): SeedState | null {
  const file = process.env.E2E_STATE;
  if (!file) return null;
  let text: string;
  try {
    text = nodeFs().readFileSync(file, "utf8");
  } catch {
    throw new Error(`E2E_STATE=${file} cannot be read`);
  }
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as SeedState;
  } catch {
    /* reported below */
  }
  throw new Error(`E2E_STATE=${file} is not a JSON object`);
}

// ── Recordings ──────────────────────────────────────────────────────────────

/** What scripts/record-har.ts writes beside each role's HAR. */
export interface Recording {
  role: Role;
  /** The instant the pages' clock was held at while recording; replay holds
   *  it there too, so every URL built from the time comes out the same. */
  t0: string;
  origin: { app: string; admin: string };
  routes: string[];
  skipped: { id: string; missing: string[] }[];
  unsettled: { id: string; viewport: string; reason: string }[];
  viewports: string[];
  playwright: string;
  browser: string;
}

/** A role's HAR and the recording's description, in `dir`. */
export function recordingFiles(dir: string, role: Role): { har: string; sidecar: string } {
  const path = nodePath();
  return { har: path.join(dir, `${role}.har`), sidecar: path.join(dir, `${role}.json`) };
}

// ── Staging a page ──────────────────────────────────────────────────────────

/** Context options every visit shares, recorded or replayed. The locale and
 *  time zone are pinned because dates on screen, and in some request URLs, are
 *  formatted in them. */
export const PAGE_CONTEXT = {
  locale: "en-US",
  timezoneId: "UTC",
  colorScheme: "light",
  reducedMotion: "reduce",
  serviceWorkers: "block",
} as const;

/** Written before any page script runs, on every document. */
const PINNED_STORAGE: Record<string, string> = {
  // The console's theme, stored raw (ThemeContext, theme-boot.js).
  "retina.theme": "light",
  // The map's basemap, stored as JSON (useMapPreference): OpenStreetMap,
  // chosen by hand, so no theme swaps it for another.
  "retina.map.tile.theme": JSON.stringify("osm"),
  "retina.map.tile.chosenBy": JSON.stringify("hand"),
};

/** Every basemap host the console draws from (pages/map/utils/tiles.ts). */
const TILE_HOSTS = [/(^|\.)basemaps\.cartocdn\.com$/, /^tile\.openstreetmap\.org$/];

/** The one tile every basemap request is answered with: 1×1, opaque, the
 *  light grey of an empty map. */
const TILE_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mN4+vg+AAVYAqhMoaMHAAAAAElFTkSuQmCC";

let tileBytes: Uint8Array | null = null;
function tilePng(): Uint8Array {
  tileBytes ??= nodeProcess.getBuiltinModule("node:buffer").Buffer.from(TILE_PNG, "base64");
  return tileBytes;
}

let leaflet: { version: string; dist: string } | null | undefined;
/** The installed Leaflet's dist folder, which the bundle runs on. */
function leafletDist(): { version: string; dist: string } | null {
  if (leaflet !== undefined) return leaflet;
  const path = nodePath();
  const dir = path.join(repoRoot(), "node_modules", "leaflet");
  try {
    const { version } = JSON.parse(nodeFs().readFileSync(path.join(dir, "package.json"), "utf8")) as { version: string };
    leaflet = { version, dist: path.join(dir, "dist") };
  } catch {
    leaflet = null;
  }
  return leaflet;
}

/**
 * The local copy of a file index.html loads from unpkg (Leaflet's stylesheet,
 * and the images it names), or null. Served only when the installed Leaflet
 * is the version the URL asks for, so the bytes are the ones unpkg would send.
 */
function leafletFile(url: URL): string | null {
  const match = /^\/leaflet@([^/]+)\/dist\/([A-Za-z0-9._/-]+)$/.exec(url.pathname);
  if (url.hostname !== "unpkg.com" || !match || match[2].split("/").includes("..")) return null;
  const installed = leafletDist();
  if (!installed || installed.version !== match[1]) return null;
  const file = nodePath().join(installed.dist, match[2]);
  return nodeFs().existsSync(file) ? file : null;
}

/** What a staged page did that the caller reports. */
export interface PageWatch {
  /** Requests sent and not yet answered; the clock does not move while any is.
   *  In a replay, an API request stays here until the page has also taken its
   *  answer in (answerInTurn). */
  readonly inflight: Set<Request>;
  /** Requests for another origin, aborted. */
  readonly offsite: string[];
  /** API requests that failed: in a replay, mostly ones the HAR has no answer for. */
  readonly failedApi: string[];
  /** Uncaught page errors, from scripts and from timers the clock ran. */
  readonly errors: string[];
}

function note(list: string[], entry: string): void {
  if (list.length < 50 && !list.includes(entry)) list.push(entry);
}

/** Pins storage and counts the DOM changes that can move a pixel. Runs in the
 *  page, before its own scripts, so it may use nothing from this module. */
function pinPage(storage: Record<string, string>): void {
  try {
    for (const [key, value] of Object.entries(storage)) window.localStorage.setItem(key, value);
  } catch {
    /* no storage: the page draws its defaults */
  }
  let changes = 0;
  // A write of the value already there is no change. Leaflet and react-leaflet
  // rewrite unchanged attributes on every frame, and counting those would
  // keep a still page from ever reading as settled. Inside the tile pane
  // nothing counts: tiles are hidden from the screenshots.
  const counts = (record: MutationRecord): boolean => {
    const target = record.target;
    const element = target.nodeType === 1 ? (target as Element) : target.parentElement;
    if (element && element.closest(".leaflet-tile-pane")) return false;
    if (record.type === "attributes") return (target as Element).getAttribute(record.attributeName ?? "") !== record.oldValue;
    if (record.type === "characterData") return (target as CharacterData).data !== record.oldValue;
    return true;
  };
  new MutationObserver((records) => {
    for (const record of records) if (counts(record)) changes += 1;
  }).observe(document, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeOldValue: true,
    characterData: true,
    characterDataOldValue: true,
  });
  Object.defineProperty(window, "__pageChanges", { get: () => changes });
}

/**
 * Stages `page` for a visit, before it navigates anywhere.
 *
 * - The clock is held at `t0` and its timers run only when settlePage moves
 *   them, so whatever the page computes from the time, the moment it polls
 *   and how far an animation got are the same on every run.
 * - Storage holds the light theme and the OpenStreetMap basemap.
 * - Every WebSocket is answered in the page and never reaches a server, so no
 *   live feed draws anything.
 * - With `har`, the API is answered from it, one answer at a time in the
 *   order the page asked (answerInTurn), and a request it has no answer for
 *   is aborted.
 * - Every basemap tile is one fixed PNG, Leaflet's stylesheet comes from the
 *   installed package, and any other request that would leave the dev
 *   server's origins is aborted and listed in the returned watch.
 */
export async function stagePage(page: Page, options: { t0: number; har?: string }): Promise<PageWatch> {
  const watch: PageWatch = { inflight: new Set(), offsite: [], failedApi: [], errors: [] };
  const sites = localSites();
  const origins = new Set<string>([sites.app, sites.admin]);
  // Replayed answers the page has not taken in yet, each with what tells
  // answerInTurn its last byte (or its failure) has landed: they stay in
  // flight until answerInTurn lets them go.
  const takingIn = new Map<Request, () => void>();
  const answered = (request: Request) => {
    const arrived = takingIn.get(request);
    if (arrived) arrived();
    else watch.inflight.delete(request);
  };

  page.on("request", (request) => watch.inflight.add(request));
  page.on("requestfinished", answered);
  page.on("requestfailed", (request) => {
    answered(request);
    const url = new URL(request.url());
    if (origins.has(url.origin) && url.pathname.startsWith("/api/")) {
      note(watch.failedApi, `${request.method()} ${url.pathname}${url.search} (${request.failure()?.errorText ?? "failed"})`);
    }
  });
  page.on("pageerror", (error) => note(watch.errors, error.message));

  // Paused first, then fixed: replayed in this order on each new document,
  // the clock stays paused and Date stays at t0 however far the timers run.
  await page.clock.pauseAt(options.t0);
  await page.clock.setFixedTime(options.t0);
  await page.addInitScript(pinPage, PINNED_STORAGE);
  await page.routeWebSocket(/.*/, () => {});
  // Registered before the catch-all below, which runs first and falls back
  // to it for the dev server's own requests.
  if (options.har) await page.routeFromHAR(options.har, { url: /\/api\//, notFound: "abort" });
  const inTurn = options.har ? answerInTurn(page, watch, takingIn) : null;
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (origins.has(url.origin)) {
      if (inTurn && url.pathname.startsWith("/api/")) await inTurn(route.request());
      return route.fallback();
    }
    if (TILE_HOSTS.some((host) => host.test(url.hostname))) {
      return route.fulfill({ status: 200, contentType: "image/png", body: tilePng() });
    }
    const file = leafletFile(url);
    if (file) return route.fulfill({ path: file });
    note(watch.offsite, url.href);
    return route.abort("blockedbyclient");
  });
  return watch;
}

/** Message round trips per look at a page taking an answer in: deep enough
 *  for an answer's renders, their effects and the renders those set off. */
const DRAIN_HOPS = 5;
/** Real time a page may go on changing after an answer before the next one
 *  is let in regardless; one that never stops fails its settling anyway. */
const TAKE_IN_MS = 2_000;

/** Lets the page run what it has queued, and what that queues in turn, for
 *  `hops` message round trips, then reads its DOM change count. The clock
 *  holds timers, not messages, and React renders and runs effects on
 *  messages. Runs in the page, so it may use nothing from this module. */
function drainPage(hops: number): Promise<number> {
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    let left = hops;
    channel.port1.onmessage = () => {
      left -= 1;
      if (left > 0) {
        channel.port2.postMessage(null);
        return;
      }
      channel.port1.close();
      resolve((window as unknown as { __pageChanges?: number }).__pageChanges ?? -1);
    };
    channel.port2.postMessage(null);
  });
}

/** Resolves once an answer has `arrived`, in full or as a failure, and the
 *  page has taken it in: what it set off has run and the DOM stayed the same
 *  from one look to the next. Never rejects: a page or document that went
 *  away has nothing left to take in. */
async function takenIn(page: Page, arrived: Promise<void>): Promise<void> {
  await arrived;
  const deadline = Date.now() + TAKE_IN_MS;
  let last = Number.NaN;
  while (Date.now() < deadline) {
    const changes = await page.evaluate(drainPage, DRAIN_HOPS).catch(() => null);
    if (changes === null || changes === last) return;
    last = changes;
    await pause(SETTLE_POLL_MS);
  }
}

/**
 * Lets a replay's API answers into the page one at a time, in the order the
 * page asked for them: each goes through once the one before has been taken
 * in, and stays in flight, holding the clock, until it has been taken in too.
 *
 * Answered as each lookup in the HAR returns, answers asked for together
 * would land in whichever order their lookups finished, and a page that draws
 * from more than one would draw it differently from run to run. The
 * Contribution chart did: it draws from the network's analytics and, once the
 * owner's nodes are known, from the owner's own, and recharts sizes the
 * labels behind its grid lines once, at a font size that depends on whether
 * the chart first drew from one answer or from both.
 *
 * Only a replay is held to it: a recording takes the live answers as they
 * come, and the replay finds each one by its URL whatever their order.
 *
 * Returns what the route handler awaits before it lets `request` through.
 */
function answerInTurn(
  page: Page,
  watch: PageWatch,
  takingIn: Map<Request, () => void>,
): (request: Request) => Promise<void> {
  let previous: Promise<void> = Promise.resolve();
  return async (request) => {
    // Set before its turn, so a request the page drops while it waits has
    // arrived, as a failure, by the time its turn comes.
    const arrived = new Promise<void>((resolve) => {
      takingIn.set(request, () => resolve());
    });
    const turn = previous;
    let done!: () => void;
    previous = new Promise<void>((resolve) => {
      done = () => resolve();
    });
    await turn;
    // Not awaited: the route handler has to return before the HAR answers.
    void takenIn(page, arrived).then(() => {
      takingIn.delete(request);
      watch.inflight.delete(request);
      done();
    });
  };
}

// ── Settling ────────────────────────────────────────────────────────────────

/** How far the page's clock moves per step, in its own milliseconds. */
const STEP_MS = 100;
/** Steps in a row with nothing in flight, nothing pending and no DOM change
 *  before a page counts as settled. */
const QUIET_STEPS = 5;
/** Real time between steps, for the page to start whatever the step woke. */
const SETTLE_POLL_MS = 50;
export const FIRST_SETTLE_MS = 60_000;
const SCROLL_SETTLE_MS = 15_000;

/** What still keeps the page from its settled look, listed for the error. Runs
 *  in the page, so it may use nothing from this module. */
function probePage(): { changes: number; pending: string[] } {
  const pending: string[] = [];
  const counter = (window as unknown as { __pageChanges?: number }).__pageChanges;
  if (counter === undefined) pending.push("no change counter in the page");
  if (document.readyState !== "complete") pending.push(`document ${document.readyState}`);
  if (document.fonts.status !== "loaded") pending.push("fonts loading");
  const root = document.getElementById("root");
  if (!root || root.childElementCount === 0) pending.push("the app has drawn nothing");

  // On screen: in the viewport, with a size, not hidden.
  const shown = (element: Element): boolean => {
    const box = element.getBoundingClientRect();
    if (box.width <= 0 || box.height <= 0) return false;
    if (box.bottom <= 0 || box.right <= 0 || box.top >= window.innerHeight || box.left >= window.innerWidth) return false;
    const style = getComputedStyle(element);
    return style.visibility !== "hidden" && style.display !== "none";
  };

  if (document.querySelector(".loading-screen")) pending.push("loading screen");
  // "Loading…", "Loading the fleet…": text the console shows while it fetches.
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set<Element>();
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement;
    if (!parent || seen.has(parent) || !/^\s*Loading\b/.test(node.nodeValue ?? "")) continue;
    seen.add(parent);
    const text = (parent.textContent ?? "").trim();
    if (text.length <= 120 && /^Loading\b[\s\S]*(…|\.\.\.)$/.test(text) && shown(parent)) pending.push(`"${text}"`);
  }
  for (const busy of Array.from(document.querySelectorAll('[aria-busy="true"]'))) {
    if (shown(busy)) pending.push(`busy <${busy.tagName.toLowerCase()} class="${busy.getAttribute("class") ?? ""}">`);
  }
  for (const map of Array.from(document.querySelectorAll(".leaflet-container"))) {
    if (!shown(map)) continue;
    const tiles = Array.from(map.querySelectorAll("img.leaflet-tile")) as HTMLImageElement[];
    if (tiles.length === 0) pending.push("a map without tiles");
    else if (tiles.some((tile) => !tile.complete)) pending.push("map tiles loading");
  }
  for (const chart of Array.from(document.querySelectorAll(".recharts-responsive-container"))) {
    if (shown(chart) && !chart.querySelector("svg.recharts-surface")) pending.push("a chart not drawn yet");
  }
  for (const image of Array.from(document.images)) {
    if (!image.complete && shown(image)) {
      pending.push(`image ${image.currentSrc || image.src}`);
      break;
    }
  }
  return { changes: counter ?? -1, pending };
}

const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** How settlePage ends a budget that runs out. */
export interface SettleOptions {
  /**
   * Called in place of the error when the budget runs out on a page that has
   * loaded everything, nothing in flight and nothing pending when looked at
   * once more, and only its DOM never stayed unchanged for QUIET_STEPS steps.
   * settlePage then returns with the page as it stands. Without it, such a
   * page fails like any other.
   */
  onStillChanging?: () => void;
}

/**
 * Moves the page's clock until it has settled: nothing in flight, no loading
 * text or busy region on screen, every visible map with its tiles and every
 * visible chart drawn, and no DOM change across QUIET_STEPS steps.
 *
 * The clock moves only while no request is in flight, so an answer always
 * lands at the same point of the page's own time, however long it took to
 * arrive. Settled, the clock stays where it is, and nothing the page runs on
 * a timer moves again until the next call.
 */
export async function settlePage(page: Page, watch: PageWatch, budgetMs: number, options: SettleOptions = {}): Promise<void> {
  const deadline = Date.now() + budgetMs;
  let lastChanges = Number.NaN;
  let quiet = 0;
  let pending: string[] = ["never probed"];
  while (Date.now() < deadline) {
    if (watch.inflight.size > 0) {
      quiet = 0;
      await pause(SETTLE_POLL_MS);
      continue;
    }
    // Null while a navigation replaces the document.
    const probe = await page.evaluate(probePage).catch((error: unknown) => {
      pending = [`the page could not be probed: ${String(error instanceof Error ? error.message : error).split("\n")[0]}`];
      return null;
    });
    if (probe === null) {
      quiet = 0;
      await pause(SETTLE_POLL_MS);
      continue;
    }
    pending = probe.pending;
    quiet = pending.length === 0 && probe.changes === lastChanges ? quiet + 1 : 0;
    lastChanges = probe.changes;
    if (quiet >= QUIET_STEPS) return;
    try {
      await page.clock.runFor(STEP_MS);
    } catch (error) {
      // The first error a timer threw while the clock ran; the clock still
      // reached the end of the step.
      note(watch.errors, String(error instanceof Error ? error.message : error).split("\n")[0]);
    }
    await pause(SETTLE_POLL_MS);
  }
  if (options.onStillChanging && watch.inflight.size === 0) {
    // Looked at again: the last step may have waited on a request rather
    // than probed the page.
    const last = await page.evaluate(probePage).catch(() => null);
    if (last && last.pending.length === 0 && watch.inflight.size === 0) {
      options.onStillChanging();
      return;
    }
    if (last) pending = last.pending;
  }
  const inflight = Array.from(watch.inflight)
    .slice(0, 5)
    .map((request) => `in flight: ${request.method()} ${request.url()}`);
  throw new Error(`the page did not settle within ${budgetMs / 1000} s: ${[...pending, ...inflight].join("; ")}`);
}

// ── Screens ─────────────────────────────────────────────────────────────────

/** Screens per page at most: the first, then five more down its scroll. */
export const MAX_SCREENS = 6;

/** The console's scrolling pane, or the document when it is the one that
 *  scrolls, or null when nothing does. Runs in the page. */
function findScroller(): Element | null {
  const scrolls = (element: Element | null, root: boolean): boolean => {
    if (!element || element.scrollHeight - element.clientHeight < 1) return false;
    const overflow = getComputedStyle(element).overflowY;
    return root ? overflow !== "hidden" && overflow !== "clip" : overflow === "auto" || overflow === "scroll";
  };
  const pane = document.querySelector(".content");
  if (scrolls(pane, false)) return pane;
  const root = document.scrollingElement;
  return scrolls(root, true) ? root : null;
}

/** Scrolls by exactly one screen, ignoring any smooth scrolling the page asks
 *  for, and says whether it moved. Runs in the page. */
function scrollOneScreen(element: Element): boolean {
  const before = element.scrollTop;
  element.scrollTo({ top: before + element.clientHeight, behavior: "instant" });
  return element.scrollTop !== before;
}

/**
 * Calls `onScreen(1)` on the settled page, then, while the scrolling pane
 * still moves, scrolls it by its own height, settles and calls `onScreen`
 * again, up to MAX_SCREENS. Returns how many screens there were.
 */
export async function walkScreens(page: Page, watch: PageWatch, onScreen: (index: number) => Promise<void>): Promise<number> {
  await onScreen(1);
  const handle = await page.evaluateHandle(findScroller);
  const pane = handle.asElement();
  let index = 1;
  while (pane && index < MAX_SCREENS) {
    if (!(await pane.evaluate(scrollOneScreen))) break;
    await settlePage(page, watch, SCROLL_SETTLE_MS);
    index += 1;
    await onScreen(index);
  }
  await handle.dispose();
  return index;
}

/** The file name of a page's `index`th screen. */
export function screenName(id: string, index: number): string {
  return index === 1 ? `${id}.png` : `${id}-p${index}.png`;
}

/**
 * Layers taken out of the screenshots, and why. Hidden with a stylesheet
 * rather than Playwright's `mask`, whose boxes follow an element's own bounds:
 * the tile pane is a zero-size box, so a mask over it covers nothing, and a
 * canvas renderer is padded half the map's size past each edge, so a mask
 * over it would paint over the sidebar and header beside the map. Hidden, they
 * stay clipped to the map like the pixels they replace.
 */
const UNSTABLE_LAYERS = [
  // Basemap imagery from third-party CDNs. Every tile is already one fixed
  // PNG, and under the held clock Leaflet's fade-in never lifts a tile's
  // opacity off 0, so hiding the pane changes no pixel today; it keeps the
  // screenshots off the imagery if either ever changes.
  ".leaflet-tile-pane",
  // Leaflet's canvas renderers (pages/map/LiveAircraftMap.tsx), which draw the
  // live layers outside the DOM: their redraws wait on animation frames, which
  // the settle check cannot see.
  "canvas",
];

export async function hideUnstableLayers(page: Page): Promise<void> {
  await page.addStyleTag({ content: `${UNSTABLE_LAYERS.join(", ")} { visibility: hidden !important; }` });
}
