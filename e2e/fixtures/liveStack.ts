/**
 * The local stack a spec opens console pages against: which of its two modes
 * the backend runs in, and the seeded owner's session, signed in once for
 * the whole run.
 *
 * One backend cannot serve the admin console's anonymous administrator and an
 * owner, or nobody, at once: AUTH_ALLOW_ANONYMOUS_ADMIN admits every caller as
 * the administrator, cookie or not. So the pages are checked in two runs
 * against the same database, and each worker asks /api/auth/me, with no
 * session, which one it is in:
 *
 *   admin  200 with auth_enabled false: the bypass is on, and the admin
 *          console's pages can be opened as their role.
 *   app    401: sign-in is enforced, and the owner's and the signed-out
 *          pages can.
 *
 * Any other answer is a stack that is down or misconfigured. The phase is
 * then unknown, and a spec fails every page with the answer rather than skip
 * them all.
 *
 * The owner's session follows Playwright's one-account-per-worker pattern,
 * with one account for every worker: a worker fixture yields the storage
 * state, and the owner's pages take it as their `storageState`, so each
 * project's device emulation still applies. The backend mails at most five
 * sign-in links a minute to one address, and a worker is replaced after every
 * failed test, so signing in is serialised through a lock file beside
 * E2E_OWNER_STATE: the worker holding it signs in (fixtures/ownerSession.ts),
 * which writes the session to that file, and every other worker waits, then
 * reuses the file. However many workers and projects run, a run asks for one
 * link at most, and for none while the saved session holds. A sign-in that
 * fails is recorded beside the file too, under the test runner's process, so
 * the rest of that run fails with the same reason instead of asking again,
 * and the next run ignores it.
 */
import { test as base, type Browser, type BrowserContext, type PlaywrightWorkerArgs } from "@playwright/test";
import { signInOwner } from "./ownerSession.ts";
import { insideRepository, nodePath, siteOrigin, type Role } from "./routes.ts";

type StorageState = Awaited<ReturnType<BrowserContext["storageState"]>>;

export type Phase = { kind: "admin" } | { kind: "app" } | { kind: "unknown"; answer: string };

/** The phase a role's pages are opened in. */
export const PHASE_OF: Readonly<Record<Role, "admin" | "app">> = { owner: "app", signedout: "app", admin: "admin" };

const firstLine = (error: unknown) => (error instanceof Error ? error.message : String(error)).split("\n")[0];
const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** The backend's phase, from /api/auth/me asked with no session. */
async function askPhase(playwright: PlaywrightWorkerArgs["playwright"]): Promise<Phase> {
  const url = `${siteOrigin("app")}/api/auth/me`;
  const request = await playwright.request.newContext();
  try {
    const response = await request.get(url, { timeout: 15_000 });
    const status = response.status();
    if (status === 401) return { kind: "app" };
    const text = await response.text();
    if (status === 200) {
      let body: { auth_enabled?: unknown } | null = null;
      try {
        body = JSON.parse(text) as { auth_enabled?: unknown };
      } catch {
        /* named below */
      }
      if (body?.auth_enabled === false) return { kind: "admin" };
    }
    return { kind: "unknown", answer: `GET ${url} answered ${status}: ${text.replace(/\s+/g, " ").slice(0, 200)}` };
  } catch (error) {
    return { kind: "unknown", answer: `GET ${url} got no answer: ${firstLine(error)}` };
  } finally {
    await request.dispose();
  }
}

/** Fails, naming the answer, when the backend's phase is neither. */
export function requireKnownPhase(phase: Phase): void {
  if (phase.kind !== "unknown") return;
  throw new Error(
    `${phase.answer}. That is neither the admin phase (200 with auth_enabled false, under ` +
      "AUTH_ALLOW_ANONYMOUS_ADMIN=1) nor the app phase (401 to a caller without a session), " +
      "so no page can be opened as its role",
  );
}

// ── The shared sign-in ──────────────────────────────────────────────────────
// The e2e program carries no Node types (see specs/env.d.ts), so the few
// process and file calls the lock needs are typed here.

interface LockFs {
  openSync(path: string, flags: "wx"): number;
  writeSync(fd: number, text: string): number;
  closeSync(fd: number): void;
  existsSync(path: string): boolean;
  readFileSync(path: string, encoding: "utf8"): string;
  writeFileSync(path: string, data: string): void;
  rmSync(path: string, options: { force: true }): void;
  mkdirSync(path: string, options: { recursive: true }): void;
}

interface LockProcess {
  pid: number;
  /** The test runner, which every worker of one run is a child of. */
  ppid: number;
  kill(pid: number, signal: 0): true;
  getBuiltinModule(id: "node:fs"): LockFs;
}

const runtime = process as unknown as LockProcess;
const lockFs = (): LockFs => runtime.getBuiltinModule("node:fs");
const codeOf = (error: unknown) => (error as { code?: unknown } | null)?.code;

/** How often a waiting worker looks at the lock, and for how long in all: a
 *  sign-in waits up to 20 s for its mail. */
const LOCK_POLL_MS = 500;
const LOCK_WAIT_MS = 120_000;

function alive(pid: number): boolean {
  try {
    runtime.kill(pid, 0);
    return true;
  } catch (error) {
    return codeOf(error) === "EPERM";
  }
}

function readOr(file: string, fallback: string): string {
  try {
    return lockFs().readFileSync(file, "utf8");
  } catch {
    return fallback;
  }
}

/** Takes the lock, or says it is held. A lock whose holder has died, killed
 *  mid sign-in, is cleared for the next look. */
function takeLock(lock: string): boolean {
  const fs = lockFs();
  try {
    const fd = fs.openSync(lock, "wx");
    try {
      fs.writeSync(fd, String(runtime.pid));
    } finally {
      fs.closeSync(fd);
    }
    return true;
  } catch (error) {
    if (codeOf(error) !== "EEXIST") throw error;
  }
  const holder = readOr(lock, "");
  const pid = Number.parseInt(holder, 10);
  if (Number.isInteger(pid) && pid > 0 && !alive(pid) && readOr(lock, "") === holder) fs.rmSync(lock, { force: true });
  return false;
}

/** The reason this run's sign-in already failed, or null. */
function failedInThisRun(marker: string): string | null {
  const text = readOr(marker, "");
  if (text === "") return null;
  const [run, ...reason] = text.split("\n");
  if (run === String(runtime.ppid)) return reason.join("\n");
  lockFs().rmSync(marker, { force: true });
  return null;
}

/** The owner's session for this worker, signed in once per run and shared. */
async function sharedOwnerState(browser: Browser): Promise<StorageState> {
  const named = process.env.E2E_OWNER_STATE;
  if (!named) {
    throw new Error(
      "E2E_OWNER_STATE is not set. The owner's pages need it: it names the file, outside the repository, " +
        "where the one sign-in of a run is kept for every worker. Without it each worker, and each one " +
        "replaced after a failure, would ask for a link of its own, and the backend stops mailing after five a minute",
    );
  }
  const stateFile = nodePath().resolve(named);
  if (insideRepository(stateFile)) {
    throw new Error(`E2E_OWNER_STATE=${named} is inside the repository, which is public: keep sessions outside it`);
  }
  const fs = lockFs();
  fs.mkdirSync(nodePath().dirname(stateFile), { recursive: true });
  const lock = `${stateFile}.lock`;
  const marker = `${stateFile}.failed`;

  const deadline = Date.now() + LOCK_WAIT_MS;
  while (!takeLock(lock)) {
    if (Date.now() > deadline) {
      throw new Error(`${lock} was still held after ${LOCK_WAIT_MS / 1000} s; delete it if no run is signing in`);
    }
    await pause(LOCK_POLL_MS);
  }
  try {
    const earlier = failedInThisRun(marker);
    if (earlier !== null) throw new Error(`the owner's sign-in already failed in this run, so it is not asked again: ${earlier}`);
    const context = await browser.newContext();
    try {
      return (await signInOwner(context, siteOrigin("app"))).storageState;
    } catch (error) {
      fs.writeFileSync(marker, `${runtime.ppid}\n${firstLine(error)}`);
      throw error;
    } finally {
      await context.close();
    }
  } finally {
    fs.rmSync(lock, { force: true });
  }
}

/**
 * The owner's session as the owner's pages take it. The backend marks its
 * cookie Secure, and the local stack is plain http: Chromium counts
 * http://localhost as a secure origin and sends a Secure cookie to it, but
 * Playwright's WebKit does not, so every owner page there would open signed
 * out. Over http, the cookies of localhost and its subdomains are handed to
 * the pages without the flag; over https nothing changes. The saved file
 * keeps the cookie as the backend set it.
 */
function forPagesOfThisStack(state: StorageState): StorageState {
  if (new URL(siteOrigin("app")).protocol !== "http:") return state;
  const local = (domain: string) => /^\.?(.+\.)?localhost$/i.test(domain);
  return { ...state, cookies: state.cookies.map((cookie) => (local(cookie.domain) ? { ...cookie, secure: false } : cookie)) };
}

interface LiveStack {
  /** The backend's phase, asked once per worker. */
  phase: Phase;
  /** The owner's session in the app phase, as its pages take it, and null in any other. */
  ownerState: StorageState | null;
}

export const test = base.extend<object, LiveStack>({
  phase: [
    async ({ playwright }, use) => {
      await use(await askPhase(playwright));
    },
    { scope: "worker" },
  ],
  ownerState: [
    async ({ browser, phase }, use) => {
      await use(phase.kind === "app" ? forPagesOfThisStack(await sharedOwnerState(browser)) : null);
    },
    // Waiting for another worker's sign-in, then signing in, may take longer
    // than a test.
    { scope: "worker", timeout: LOCK_WAIT_MS + 60_000 },
  ],
});
