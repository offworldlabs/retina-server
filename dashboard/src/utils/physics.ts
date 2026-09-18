/**
 * The physics layer draws the synthetic fleet's solver internals, so it exists
 * only where the server runs a fleet, and only for a signed-in user. The server
 * says which on /api/auth/me.
 */
export function showsPhysics(user: { synthetic_fleet?: boolean } | null): boolean {
  return Boolean(user?.synthetic_fleet);
}
