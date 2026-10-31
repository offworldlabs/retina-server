/**
 * Links from this bundle to the other surfaces in the same environment.
 *
 * A hard-coded production hostname is wrong here: from a staging or test page
 * it points at production, so a link meant to move between surfaces moves
 * between environments instead. The sibling's name is derived from this one
 * (`staging-app` becomes `staging-towers`). Anything whose first label is not a
 * recognisable surface (a dev server on bare `localhost`, an IP, a preview)
 * falls back to production, the only family whose names are certain from here.
 */

import { RETINA_DOMAIN } from "./community";

// The surface names this bundle can be served under, as the first label of the
// host: `admin`, `app`, and their environment-prefixed forms.
const SURFACE_LABEL = /(^|-)(admin|app)$/;

// `host` rather than `hostname`, so the port survives: the laptop stack reaches
// every vhost on :8080, and a sibling named without it is a different origin
// that nothing is listening on.
function siblingOrigin(role: string, host: string, protocol: string): string {
  const first = host.split(".")[0];
  if (SURFACE_LABEL.test(first) && host.includes(".")) {
    return `${protocol}//${host.replace(first, first.replace(SURFACE_LABEL, `$1${role}`))}`;
  }
  return `https://${role}.${RETINA_DOMAIN}`;
}

/**
 * The tower finder, which is tower-finder-service's surface and never shares an
 * origin with this bundle, so it is always absolute.
 */
export function towerFinderUrl(host: string, protocol: string): string {
  return `${siblingOrigin("towers", host, protocol)}/`;
}

/**
 * The app console's front page, for a caller the admin console turns away. This
 * bundle serves it too, but under its own hostname, which picks the surface, so
 * a relative link would only lead back to the admin console.
 */
export function appConsoleUrl(host: string, protocol: string): string {
  return `${siblingOrigin("app", host, protocol)}/`;
}
