/**
 * Which of the two surfaces this bundle renders. Sibling of domains.ts, but
 * not symmetric with it: ADMIN_HOST below takes any environment prefix, while
 * the app hosts there are a fixed staging-|test- allowlist. A new environment
 * needs that file edited and not this one, and until it is, its /map silently
 * gets the wrong default.
 */

// Any environment prefix, not an allowlist of the three that exist today: an
// unknown prefix must fall through to the admin console rather than silently
// render the user dashboard on an admin vhost. The dev server is no exception:
// it answers on every `*.localhost` name, so `admin.localhost` selects it there.
const ADMIN_HOST = /^(?:[a-z0-9-]+-)?admin\./i;

type Surface = {
  isAdmin: boolean;
  /** `?mode=admin` was asked for on a host that is not an admin one. */
  modeParamIgnored: boolean;
};

export function isAdminHost(hostname: string): boolean {
  return ADMIN_HOST.test(hostname);
}

// Exported so the tests assert against the same string this emits, rather than
// each carrying its own copy of a substring of it.
export const MODE_IGNORED_WARNING =
  "?mode=admin does nothing: the hostname selects the admin console. Open this environment's admin vhost (admin.localhost on the dev server).";

// Kept beside the predicate that produces the flag, so it can be tested
// without loading App and everything App imports.
export function warnIfModeIgnored(modeParamIgnored: boolean): void {
  if (!modeParamIgnored) return;
  console.warn(MODE_IGNORED_WARNING);
}

export function resolveSurface(hostname: string, search: string): Surface {
  const isAdmin = isAdminHost(hostname);
  const wantsAdmin = new URLSearchParams(search).get("mode") === "admin";
  return { isAdmin, modeParamIgnored: wantsAdmin && !isAdmin };
}
