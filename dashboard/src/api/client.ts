import { UnauthorizedError, request as sharedRequest, type RequestOptions } from "@retina/shared";

import { withBase } from "../utils/basePath";

/** Must match the route in App.tsx. Mounted, because this drives a full-page
 *  navigation rather than a router one: under `/dash/` a bare `/login` lands on
 *  the app vhost's root, which is the MAP bundle. */
const LOGIN_PATH = withBase("/login");

/** Trailing slashes trimmed: the router matches `/login/` to the same route, so
 *  comparing the raw pathname would send a caller who arrived that way through
 *  one more reload before the guard below started holding. */
function onLoginPage() {
  return window.location.pathname.replace(/\/+$/, "") === LOGIN_PATH;
}

// The shared client answers a 401 with UnauthorizedError; sending the caller to
// the login page is this app's decision, made here beside the route it names.
// Not when already on the login page: assigning the same URL reloads it, the
// reload re-runs this request, and its 401 assigns it again; where nothing can
// mint a session that does not terminate.
function request(path: string, opts?: RequestOptions) {
  return sharedRequest(path, opts).catch((e) => {
    if (e instanceof UnauthorizedError && !onLoginPage()) {
      window.location.href = LOGIN_PATH;
    }
    throw e;
  });
}

export const api = {
  // Auth. Who the caller is comes from the shared useCurrentUser, which goes
  // straight to the shared client: a 401 there is the answer it wants, and
  // RequireAuth routes on it without the full page load this wrapper costs.
  logout: () => request("/api/auth/logout", { method: "POST" }),

  // Magic-link sign-in. Both answer before there is a session to lose, so the
  // 401 redirect above never fires for them; they go through the wrapper for
  // one client shape rather than for it.
  //
  // requestMagicLink resolves 202 whatever the address is, so a caller cannot
  // learn from it whether an account exists. It rejects with an HttpError for
  // a malformed address (422) and for a deployment with no mail configured
  // (503) — the only two failures a page may repeat back.
  requestMagicLink: (email) =>
    request("/api/auth/magic-link", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  // Resolves {user} and leaves the session cookie behind it; rejects 400 with
  // one detail for unknown, expired and already-redeemed alike.
  consumeMagicLink: (token) =>
    request("/api/auth/magic-link/consume", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  // Self-service node ownership
  myNodes: () => request("/api/auth/me/nodes"),
  myClaimCodes: () => request("/api/auth/me/claim-codes"),
  createClaimCode: () => request("/api/auth/me/claim-codes", { method: "POST" }),
  revokeClaimCode: (code) =>
    request(`/api/auth/me/claim-codes/${encodeURIComponent(code)}`, { method: "DELETE" }),

  // Location privacy (owner). A node the caller does not own is a 404 here,
  // not a 403 — the id space is guessable and the two answers would differ
  // only in confirming the id exists.
  myNodeLocationPrivacy: (nodeId, isPrivate) =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/location-privacy`, {
      method: "PUT",
      body: JSON.stringify({ private: isPrivate }),
    }),
  clearMyNodeLocationPrivacy: (nodeId) =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/location-privacy`, {
      method: "DELETE",
    }),

  // Radar / nodes
  nodes: () => request("/api/radar/nodes"),
  status: () => request("/api/radar/status"),
  analytics: () => request("/api/radar/analytics"),
  nodeAnalytics: (id) => request(`/api/radar/analytics/${id}`),
  aircraft: () => request("/api/radar/data/aircraft.json"),
  overlaps: () => request("/api/radar/association/overlaps"),
  anomalies: () => request("/api/radar/anomalies"),

  // Archive
  archive: (limit = 50, offset = 0, nodeId = null) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (nodeId) params.set("node_id", nodeId);
    return request(`/api/data/archive?${params}`);
  },

  // Custody
  custody: () => request("/api/custody/status"),

  // Test dashboard (fleet overview)
  fleetDashboard: () => request("/api/test/dashboard"),

  // Leaderboard & alerts (user-facing)
  leaderboard: () => request("/api/admin/leaderboard"),
  alerts: () => request("/api/admin/alerts"),

  // Admin
  adminUsers: () => request("/api/admin/users"),
  adminSetRole: (uid, role) =>
    request(`/api/admin/users/${uid}/role`, {
      method: "PUT",
      body: JSON.stringify({ role }),
    }),
  adminEvents: (limit = 200) => request(`/api/admin/events?limit=${limit}`),
  adminNodeConfig: () => request("/api/admin/config/nodes"),
  adminTowerConfig: () => request("/api/admin/config/towers"),
  adminUpdateNodeConfig: (config) =>
    request("/api/admin/config/nodes", {
      method: "PUT",
      body: JSON.stringify({ config }),
    }),
  adminConfigHistory: () => request("/api/admin/config/history"),
  adminStorage: () => request("/api/admin/storage"),
  adminMetrics: () => request("/api/admin/metrics"),
  adminInfrastructure: () => request("/api/admin/infrastructure"),

  // MLAT verification — aggregated solver-vs-truth stats
  mlatVerification: () => request("/api/test/mlat-verification"),
  mlatAccuracy: () => request("/api/test/mlat-accuracy"),

  // Admin: invites
  adminInvites: () => request("/api/admin/invites"),
  adminCreateInvite: (email, role) =>
    request("/api/admin/invites", {
      method: "POST",
      body: JSON.stringify({ email, role }),
    }),
  adminRevokeInvite: (token) =>
    request(`/api/admin/invites/${encodeURIComponent(token)}`, { method: "DELETE" }),

  // Admin: node identity. {node_ref: node_id} for the fleet — the one route
  // that crosses the publication boundary, which is why it is admin-only.
  adminNodeRefs: () => request("/api/admin/node-refs"),

  // Admin: node ownership
  adminNodeOwners: () => request("/api/admin/node-owners"),
  adminNodeContacts: () => request("/api/admin/node-contacts"),
  adminSetNodeOwner: (nodeId, userId) =>
    request(`/api/admin/nodes/${encodeURIComponent(nodeId)}/owner`, {
      method: "PUT",
      body: JSON.stringify({ user_id: userId }),
    }),

  // Admin: node location privacy. The GET also returns the raw pieces
  // (registration_choice, override) behind the effective answer.
  adminNodeLocationPrivacy: (nodeId) =>
    request(`/api/admin/nodes/${encodeURIComponent(nodeId)}/location-privacy`),
  setAdminNodeLocationPrivacy: (nodeId, isPrivate) =>
    request(`/api/admin/nodes/${encodeURIComponent(nodeId)}/location-privacy`, {
      method: "PUT",
      body: JSON.stringify({ private: isPrivate }),
    }),
  clearAdminNodeLocationPrivacy: (nodeId) =>
    request(`/api/admin/nodes/${encodeURIComponent(nodeId)}/location-privacy`, {
      method: "DELETE",
    }),
};
