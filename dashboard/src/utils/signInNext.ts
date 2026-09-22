/**
 * The page to open once signed in, if `path` names one.
 *
 * The same rule the server holds the mailed link's `next` to (routes/auth.py):
 * a plain path on this console, and never `//`, which a browser reads as
 * another host. Checked here as well because the value arrives in a URL that
 * anyone can edit, and a request the server would drop should not be made.
 */
const NEXT_PATH = /^\/(?!\/)[A-Za-z0-9/_.-]{0,200}$/;

export function signInNext(path: unknown): string | null {
  return typeof path === "string" && NEXT_PATH.test(path) ? path : null;
}
