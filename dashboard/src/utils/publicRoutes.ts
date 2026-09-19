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
   * Current in the nav at this path alone, not for everything under it.
   *
   * Openness is by first segment regardless (see `isPublicRoute`); this only
   * stops a parent's entry lighting up beside its child's. /sim wants it
   * because /sim/physics is its own entry, and without `end` NavLink would
   * mark both current at once.
   */
  end?: boolean;
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
  { path: "/sim", label: "Simulation", icon: "target", end: true, needsFleet: true },
  // The page that tunes the fleet. Its save used to be admin-only, which is
  // why /sim once opened without it; the console has no identity provider to
  // sign an operator in with, so the PUT is open now and the page goes with
  // it. Listed for the nav: openness it already has, as a child of /sim.
  { path: "/sim/physics", label: "Physics Layer", icon: "layers", needsFleet: true },
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
  // First segment only, so a nested route travels with its parent: /data/
  // 2026/09/17 is the archive listing's own deep link, and /sim/physics is a
  // setting of the simulator. Split rather than a prefix test: "/datasets"
  // starts with "/data" and is a different page.
  const first = `/${pathname.split("/").filter(Boolean)[0] ?? ""}`;
  return PUBLIC_ROUTES.some((r) => r.path === first);
}
