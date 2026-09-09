/**
 * Which of the two surfaces this bundle renders. Sibling of the map-surface
 * predicates in frontend/src/utils/domains.ts, but no longer symmetric with
 * them: ADMIN_HOST below takes any environment prefix, while isMapDomain there
 * is a fixed staging-|test- allowlist. A new environment needs that file
 * edited and not this one, and until it is, its map surface silently gets the
 * wrong defaults.
 */

// Any environment prefix, not an allowlist of the three that exist today: an
// unknown prefix must fall through to the admin console rather than silently
// render the user dashboard on an admin vhost.
const ADMIN_HOST = /^(?:[a-z0-9-]+-)?admin\./i;

// Where `?mode=admin` is honoured: anywhere the dev server can be reached. It
// serves one origin, so no hostname there can name a surface. The three cases
// are loopback, a name (`.localhost`, or `.local` from mDNS), and a private
// address, which is what `vite --host` gives a phone or a second machine.
const LOOPBACK = /^(?:localhost|127\.0\.0\.1|\[::1\])$/i;
// IPv6 arrives bracketed, and may carry a %25-escaped zone id, so only the
// prefix is anchored. fe80::/10 (link-local) spans fe80-febf; fc00::/7
// (unique-local) spans fc00-fdff. `vite --host` binds both stacks, so an
// IPv6-preferring machine reaches the dev server this way.
const PRIVATE_IPV6 = /^\[(?:fe[89ab][0-9a-f]|f[cd][0-9a-f]{2})[0-9a-f]*:/i;
const DEV_DOMAIN = /\.(?:localhost|local)$/i;
// Anchored end to end so these match addresses and not merely names that start
// the same way: 192.168.evil.com is a registrable domain, not a dev host.
// Ranges are 10/8, 172.16/12, 192.168/16, 100.64/10 (CGNAT, so Tailscale) and
// 169.254/16 (link-local). The trailing octets are spelled out rather than
// \d{1,3} so the pattern matches the ranges it is named for and not a superset
// of them; 10/8 is separate only because it takes three of them, not two.
const PRIVATE_ADDRESS =
  /^(?:10(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}|(?:172\.(?:1[6-9]|2\d|3[01])|192\.168|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])|169\.254)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){2})$/;

type Surface = {
  isAdmin: boolean;
  /** `?mode=admin` was asked for on a host that does not honour it. */
  modeParamIgnored: boolean;
};

function isDevHost(hostname: string): boolean {
  return (
    LOOPBACK.test(hostname) ||
    DEV_DOMAIN.test(hostname) ||
    PRIVATE_ADDRESS.test(hostname) ||
    PRIVATE_IPV6.test(hostname)
  );
}

// Exported so the tests assert against the same string this emits, rather than
// each carrying its own copy of a substring of it.
export const MODE_IGNORED_WARNING =
  "?mode=admin is honoured only on a development host. Use the admin vhost for this environment.";

// Kept beside the predicate that produces the flag, so it can be tested
// without loading App and everything App imports.
export function warnIfModeIgnored(modeParamIgnored: boolean): void {
  if (!modeParamIgnored) return;
  console.warn(MODE_IGNORED_WARNING);
}

export function resolveSurface(hostname: string, search: string): Surface {
  const wantsAdmin = new URLSearchParams(search).get("mode") === "admin";
  if (ADMIN_HOST.test(hostname)) return { isAdmin: true, modeParamIgnored: false };
  if (isDevHost(hostname)) return { isAdmin: wantsAdmin, modeParamIgnored: false };
  return { isAdmin: false, modeParamIgnored: wantsAdmin };
}
