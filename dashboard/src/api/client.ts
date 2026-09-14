const BASE = "";

/** A 401 from the API: an answer, not a failure to obtain one. Distinct from a
 *  network or timeout error so callers can tell "not signed in" from "no reply
 *  yet" and decline to retry the first. */
export class UnauthorizedError extends Error {
  constructor() {
    super("Unauthorized");
    this.name = "UnauthorizedError";
  }
}

/** Must match the route in App.tsx. */
const LOGIN_PATH = "/login";

/** Trailing slashes trimmed: the router matches `/login/` to the same route, so
 *  comparing the raw pathname would send a caller who arrived that way through
 *  one more reload before the guard below started holding. */
function onLoginPage() {
  return window.location.pathname.replace(/\/+$/, "") === LOGIN_PATH;
}

async function request(path: string, opts: any = {}) {
  const controller = new AbortController();
  const timeoutMs = path === "/api/auth/me" ? 30000 : 10000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`${BASE}${path}`, {
      credentials: "same-origin",
      ...opts,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...opts.headers },
    });
    clearTimeout(timer);
    if (res.status === 401) {
      // Not when already on the login page. Assigning the same URL reloads it,
      // the reload re-runs this request, and its 401 assigns it again; where
      // nothing can mint a session that does not terminate.
      if (!onLoginPage()) {
        window.location.href = LOGIN_PATH;
      }
      throw new UnauthorizedError();
    }
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
  } catch (e) {
    clearTimeout(timer);
    throw e;
  }
}

export function downloadUrl(path) {
  return `${BASE}${path}`;
}

export const api = {
  // Auth
  me: () => request("/api/auth/me"),
  logout: () => request("/api/auth/logout", { method: "POST" }),

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
