/**
 * Where this bundle is mounted, baked in by Vite's `base` at build time.
 *
 * The same dashboard is served at two different depths: at the root of the
 * admin vhost, and under `/dash/` on the consolidated app vhost, which builds
 * it a second time (`npm run build:dash`). Vite writes its asset URLs
 * absolutely from `base`, so each build already resolves its own JS and CSS
 * correctly; this is for the app's OWN paths.
 *
 * react-router applies the basename below to everything that goes through it —
 * `<Link to>`, `navigate()`, route matching. Anything that leaves the router
 * has to add the prefix itself: a full-page `window.location` navigation, or a
 * comparison against `window.location.pathname`. That is what `withBase` is
 * for. API and WebSocket paths are NOT in this space — `/api/` and `/ws/` are
 * proxied at the root of every vhost — so they stay bare.
 */

// "" at a root, "/dash" under the mount. Trailing slash stripped so callers can
// concatenate a leading-slash path without producing a double slash.
export const BASE_PATH = import.meta.env.BASE_URL.replace(/\/+$/, "");

// react-router wants "/" rather than "" for the root case.
export const ROUTER_BASENAME = BASE_PATH || "/";

/** Prefix an in-app route for use outside the router. */
export function withBase(path: string): string {
  return `${BASE_PATH}${path}`;
}

/** Read a `window.location.pathname` back into the router's space. */
export function stripBase(path: string): string {
  if (!BASE_PATH) return path;
  // Only at a segment boundary: "/dashboard" is a sibling of the mount, not a
  // page inside it, and a bare startsWith would hand back "board".
  if (path.startsWith(`${BASE_PATH}/`)) return path.slice(BASE_PATH.length);
  return path === BASE_PATH ? "/" : path;
}
