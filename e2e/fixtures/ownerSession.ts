/**
 * Signs a browser context in as the seeded owner, on the local stack only.
 *
 * Two ways in, tried in order:
 *
 *   E2E_OWNER_STATE  a storageState file holding a session from an earlier
 *                    sign-in. Used while the server still honours it; a new
 *                    sign-in is written back to it, so the next run reuses it.
 *   the mailed link  POST /api/auth/magic-link for E2E_OWNER_EMAIL (falling
 *                    back to the state's owner_email), then the newest sign-in
 *                    link the backend writes to its log (MAIL_TRANSPORT=log)
 *                    in the file E2E_MAIL_LOG names (falling back to the
 *                    state's backend_log), redeemed with the context's own
 *                    request API so the session cookie lands in that context.
 *
 * Either way it ends by asking /api/auth/me and refusing a session that is
 * not the owner's. The link's token is never printed: an error names the
 * file and the step, not the link.
 */
import type { BrowserContext } from "@playwright/test";
import { insideRepository, loadSeedState, nodeFs, nodePath } from "./routes.ts";

/** A sign-in link's path and token: secrets.token_urlsafe(32) is 43 characters. */
const LINK = /\/auth\/link\/([A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])/g;
/** What the log transport writes ahead of each message (backend/services/mail.py). */
const MESSAGE_MARK = "MAIL_TRANSPORT=log, not sent:";
const POLL_MS = 500;
const POLLS = 40;

type StorageState = Awaited<ReturnType<BrowserContext["storageState"]>>;

export interface OwnerSession {
  /** The owner's address as /api/auth/me reports it. */
  email: string;
  /** The context's state once signed in, for further contexts to start from. */
  storageState: StorageState;
}

interface Me {
  status: number;
  role?: string;
  email?: string;
  auth_enabled?: boolean;
}

async function askMe(context: BrowserContext, appOrigin: string): Promise<Me> {
  const response = await context.request.get(`${appOrigin}/api/auth/me`);
  const status = response.status();
  if (status !== 200) return { status };
  const body = (await response.json().catch(() => ({}))) as Omit<Me, "status">;
  return { status, role: body.role, email: body.email, auth_enabled: body.auth_enabled };
}

/** Why `me` is not the owner's session, or null when it is. The anonymous
 *  administrator of a bypassed backend answers 200 too, and is refused. */
function notTheOwner(me: Me, expected: string | null): string | null {
  if (me.status !== 200) return `/api/auth/me answered ${me.status}`;
  if (me.auth_enabled === false) return "the backend admits everyone as an administrator (AUTH_ALLOW_ANONYMOUS_ADMIN)";
  if (me.role === "admin") return "the session is an administrator's";
  if (expected && (me.email ?? "").toLowerCase() !== expected.toLowerCase()) return `the session is ${me.email ?? "nobody"}'s`;
  return null;
}

/** The size of `file`, or 0 when it does not exist yet. */
function sizeOf(file: string): number {
  const fs = nodeFs();
  return fs.existsSync(file) ? fs.statSync(file).size : 0;
}

/** What `file` gained past `offset`, as text. */
function readFrom(file: string, offset: number): string {
  const fs = nodeFs();
  const size = sizeOf(file);
  if (size <= offset) return "";
  const bytes = new Uint8Array(size - offset);
  const fd = fs.openSync(file, "r");
  try {
    fs.readSync(fd, bytes, 0, bytes.length, offset);
  } finally {
    fs.closeSync(fd);
  }
  return new TextDecoder().decode(bytes);
}

/** The token of the newest sign-in link in `text`, a stretch of the backend's
 *  log, addressed to `email`. A message without a To line is taken as
 *  addressed to it. */
export function newestToken(text: string, email: string): string | null {
  const messages = text.split(MESSAGE_MARK);
  for (let i = messages.length - 1; i >= 0; i--) {
    const to = /^To:\s*(.+)$/im.exec(messages[i]);
    if (to && !to[1].toLowerCase().includes(email.toLowerCase())) continue;
    let token: string | null = null;
    for (let match = LINK.exec(messages[i]); match; match = LINK.exec(messages[i])) token = match[1];
    LINK.lastIndex = 0;
    if (token) return token;
  }
  return null;
}

const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** Asks for a sign-in link, finds it in the mail log and redeems it in `context`. */
async function signInByLink(context: BrowserContext, appOrigin: string, email: string, mailLog: string): Promise<void> {
  // Only what the backend writes after the request can be the answer to it.
  const offset = sizeOf(mailLog);
  const asked = await context.request.post(`${appOrigin}/api/auth/magic-link`, { data: { email } });
  if (asked.status() !== 202) throw new Error(`asking for a sign-in link for ${email} answered ${asked.status()}`);

  let token: string | null = null;
  for (let poll = 0; poll < POLLS && token === null; poll++) {
    await pause(POLL_MS);
    // A log that shrank was rotated or truncated: read it from the start.
    token = newestToken(readFrom(mailLog, sizeOf(mailLog) < offset ? 0 : offset), email);
  }
  if (token === null) {
    throw new Error(
      `no sign-in link for ${email} reached ${mailLog} within ${(POLL_MS * POLLS) / 1000} s. ` +
        "The backend must run with MAIL_TRANSPORT=log and write to that file, and it mails nothing " +
        "past five requests a minute from one address.",
    );
  }
  const redeemed = await context.request.post(`${appOrigin}/api/auth/magic-link/consume`, { data: { token } });
  if (redeemed.status() !== 200) throw new Error(`redeeming the sign-in link answered ${redeemed.status()}`);
}

/**
 * Signs `context` in as the seeded owner against the console at `appOrigin`,
 * and returns the session. Throws unless E2E_ENV=local, and when no way in
 * yields the owner's session.
 */
export async function signInOwner(context: BrowserContext, appOrigin: string): Promise<OwnerSession> {
  if (process.env.E2E_ENV !== "local") {
    throw new Error("the owner signs in only on the local stack: set E2E_ENV=local");
  }
  const state = loadSeedState();
  const expected = process.env.E2E_OWNER_EMAIL || state?.owner_email || null;
  const stateFile = process.env.E2E_OWNER_STATE || null;
  if (stateFile && insideRepository(stateFile)) {
    throw new Error(`E2E_OWNER_STATE=${stateFile} is inside the repository, which is public: keep sessions outside it`);
  }

  let refusal: string | null = null;
  if (stateFile && nodeFs().existsSync(stateFile)) {
    const saved = JSON.parse(nodeFs().readFileSync(stateFile, "utf8")) as Partial<StorageState>;
    await context.addCookies(saved.cookies ?? []);
    const me = await askMe(context, appOrigin);
    refusal = notTheOwner(me, expected);
    if (refusal === null) return { email: me.email ?? expected ?? "", storageState: await context.storageState() };
    // Expired, revoked, or from a database seeded since: sign in afresh.
    await context.clearCookies();
  }

  const mailLog = process.env.E2E_MAIL_LOG || state?.backend_log || null;
  if (!expected || !mailLog) {
    const missing = [!expected && "E2E_OWNER_EMAIL (or owner_email in E2E_STATE)", !mailLog && "E2E_MAIL_LOG (or backend_log in E2E_STATE)"]
      .filter(Boolean)
      .join(" and ");
    throw new Error(
      (refusal ? `the session in E2E_OWNER_STATE was refused (${refusal}), and ` : "") +
        `signing in by mail needs ${missing}`,
    );
  }
  await signInByLink(context, appOrigin, expected, mailLog);
  const me = await askMe(context, appOrigin);
  refusal = notTheOwner(me, expected);
  if (refusal !== null) throw new Error(`the sign-in link did not open the owner's session: ${refusal}`);

  if (stateFile) {
    nodeFs().mkdirSync(nodePath().dirname(nodePath().resolve(stateFile)), { recursive: true });
    await context.storageState({ path: stateFile });
  }
  return { email: me.email ?? expected, storageState: await context.storageState() };
}
