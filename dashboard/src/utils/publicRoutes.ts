/**
 * The routes a visitor reaches without signing in.
 *
 * Each one is served by an endpoint that already publishes to anyone — the
 * archive listing, the radar feeds, the ref-keyed leaderboard — or by no
 * endpoint at all. Adding a route here publishes whatever it renders, so the
 * data behind it has to be through the publication boundary first.
 *
 * The signed-out sidebar reads this list as well: its entries for these paths
 * are live, and every other entry is greyed out until there is a session.
 */
export const PUBLIC_PATHS: readonly string[] = [
  "/map",
  "/data",
  "/leaderboard",
  "/knowledge",
];

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
  // 2026/09/17 is the archive listing's own deep link. Split rather than a
  // prefix test: "/datasets" starts with "/data" and is a different page.
  const first = `/${pathname.split("/").filter(Boolean)[0] ?? ""}`;
  return PUBLIC_PATHS.includes(first);
}
