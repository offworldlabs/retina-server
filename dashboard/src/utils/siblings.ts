/**
 * Links from this bundle to the other surfaces in the same environment.
 *
 * Two things make a hard-coded `https://map.retina.fm/` wrong here. It names a
 * retired hostname, and from a staging or test page it points at production —
 * so a link meant to move between surfaces moves between environments instead.
 *
 * Under the `/dash/` mount the map is a path on this very origin, and staying
 * on it is the point: the session cookie is host-only (backend/core/users.py
 * sets no cookie_domain), so a link that leaves the origin signs the user out
 * of everything it leads to. The admin vhost keeps a name of its own and serves
 * no map, so from there the sibling's name is derived from this one rather than
 * assumed: `staging-admin` → `staging-app`. Anything whose first label is not a
 * recognisable surface — a dev server on bare `localhost`, an IP, a preview —
 * falls back to production, the only family whose names are certain from here.
 */

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
  return `https://${role}.retina.fm`;
}

/**
 * The live map. `basePath` is this build's mount (see basePath.ts): non-empty
 * means the app hostname is serving us, and the map is at its root.
 */
export function mapUrl(host: string, protocol: string, basePath: string): string {
  return basePath ? "/" : `${siblingOrigin("app", host, protocol)}/`;
}

/**
 * The tower finder, which is tower-finder-service's surface and never shares an
 * origin with this bundle, so it is always absolute.
 */
export function towerFinderUrl(host: string, protocol: string): string {
  return `${siblingOrigin("towers", host, protocol)}/`;
}
