/**
 * The routes a visitor reaches without signing in.
 *
 * Each one is served by an endpoint that already publishes to anyone — the
 * archive listing, the radar feeds, the ref-keyed leaderboard — or by no
 * endpoint at all. Adding a route here publishes whatever it renders, so the
 * data behind it has to be through the publication boundary first.
 *
 * The label and icon ride along because the signed-out nav is exactly this
 * list: keeping them apart would let a route be opened without becoming
 * reachable, or be closed and go on being advertised.
 */
export type PublicRoute = {
  path: string;
  label: string;
  /** A key into the sidebar's icon set. */
  icon: string;
};

/**
 * `/map` is the SPA's own page, like the rest of this list.
 */
export const PUBLIC_ROUTES: readonly PublicRoute[] = [
  { path: "/map", label: "Map", icon: "map" },
  { path: "/data", label: "Data Explorer", icon: "database" },
  { path: "/leaderboard", label: "Leaderboard", icon: "trophy" },
  { path: "/knowledge", label: "Knowledge Base", icon: "book" },
];

export const PUBLIC_PATHS: readonly string[] = PUBLIC_ROUTES.map((r) => r.path);

/**
 * Whether `pathname` is open to a caller with no session.
 *
 * Router-space: the basename is already off, which is what `useLocation` gives
 * and what `stripBase` makes of `window.location.pathname`.
 *
 * The admin surface has none, whatever the path. Its routes are a different
 * tree, but a shared path — `/nodes/:nodeId` exists in both — would otherwise
 * turn an entry added here into a hole in the console.
 */
export function isPublicRoute(pathname: string, isAdmin: boolean): boolean {
  if (isAdmin) return false;
  // The index renders nothing of its own: it forwards to the map.
  if (pathname === "/") return true;
  // First segment only, so a nested route travels with its parent. Split rather
  // than a prefix test: "/datasets" starts with "/data" and is a different page.
  const segment = `/${pathname.split("/")[1] ?? ""}`;
  return PUBLIC_PATHS.includes(segment);
}
