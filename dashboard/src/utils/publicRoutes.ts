/**
 * The routes a visitor reaches without signing in.
 *
 * Each one is served by an endpoint that already publishes to anyone — the
 * archive listing, the radar feeds, the ref-keyed leaderboard — or by no
 * endpoint at all. Adding a route here publishes whatever it renders, so the
 * data behind it has to be through the publication boundary first.
 *
 * The label and icon ride along because the signed-out nav is drawn from this
 * list: keeping them apart would let a route be opened without becoming
 * reachable, or be closed and go on being advertised.
 */
export type PublicRoute = {
  path: string;
  label: string;
  /** A key into the sidebar's icon set. */
  icon: string;
  /**
   * Open at this path alone, rather than at everything nested under it.
   *
   * The default is the other way round, because most of these pages own a
   * subtree that is the same publication: /data/2026/09/17 is the archive
   * listing's own deep link. /sim is the exception — it has a child, /sim/
   * physics, that writes the fleet's configuration through an admin-only PUT.
   * Opening the parent must not open the child, and the honest way to say so
   * is on the parent rather than as a list of exceptions somewhere else.
   */
  exact?: boolean;
  /**
   * Advertised only where the server runs a synthetic fleet.
   *
   * Openness and advertisement come apart here, and only here. The route is
   * open everywhere — one bundle serves every environment, and a path that
   * exists on test but 404s on production would be a second hostname rule in
   * disguise, which is the thing this change removes. What varies is whether
   * pointing at it is honest: on production there is no fleet behind it. See
   * `advertisedPublicRoutes`.
   */
  needsFleet?: boolean;
};

/**
 * `/map` is the SPA's own page, like the rest of this list.
 */
export const PUBLIC_ROUTES: readonly PublicRoute[] = [
  { path: "/map", label: "Map", icon: "map" },
  { path: "/sim", label: "Simulation", icon: "target", exact: true, needsFleet: true },
  { path: "/data", label: "Data Explorer", icon: "database" },
  { path: "/leaderboard", label: "Leaderboard", icon: "trophy" },
  { path: "/knowledge", label: "Knowledge Base", icon: "book" },
];

export const PUBLIC_PATHS: readonly string[] = PUBLIC_ROUTES.map((r) => r.path);

/**
 * The open routes worth pointing a visitor at, given what the server runs.
 *
 * `syntheticFleet` is /api/health's answer (see AuthContext): a visitor has no
 * /api/auth/me to read it off. Default false, so a server that did not answer
 * leaves the surface unadvertised rather than advertised into an empty map —
 * anyone who has the link still reaches it.
 */
export function advertisedPublicRoutes(syntheticFleet: boolean): readonly PublicRoute[] {
  return PUBLIC_ROUTES.filter((r) => !r.needsFleet || syntheticFleet);
}

/**
 * Whether `pathname` is open to a caller with no session.
 *
 * The admin surface has none, whatever the path. Its routes are a different
 * tree, but a shared path — `/nodes/:nodeId` exists in both — would otherwise
 * turn an entry added here into a hole in the console.
 */
export function isPublicRoute(pathname: string, isAdmin: boolean): boolean {
  if (isAdmin) return false;
  // The index renders nothing of its own: it forwards to the map.
  if (pathname === "/") return true;
  // First segment only, so a nested route travels with its parent unless the
  // entry says otherwise. Split rather than a prefix test: "/datasets" starts
  // with "/data" and is a different page.
  const segments = pathname.split("/").filter(Boolean);
  const entry = PUBLIC_ROUTES.find((r) => r.path === `/${segments[0] ?? ""}`);
  if (!entry) return false;
  return !entry.exact || segments.length === 1;
}
