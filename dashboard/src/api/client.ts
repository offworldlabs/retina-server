import { UnauthorizedError, request as sharedRequest, type RequestOptions } from "@retina/shared";

import { isPublicRoute } from "../utils/publicRoutes";
import { signInNext } from "../utils/signInNext";
import { isAdminHost } from "../utils/surface";

/** Must match the route in App.tsx. */
const LOGIN_PATH = "/login";

/** Trailing slashes trimmed: the router matches `/login/` to the same route, so
 *  comparing the raw pathname would send a caller who arrived that way through
 *  one more reload before the guard below started holding. */
function onLoginPage() {
  return window.location.pathname.replace(/\/+$/, "") === LOGIN_PATH;
}

/** On a route open to a caller with no session. The path is read afresh each
 *  time, since a single-page app changes it without reloading. */
function onPublicPage() {
  const { hostname, pathname } = window.location;
  return isPublicRoute(pathname, isAdminHost(hostname));
}

/** The login page, carrying the page the session ran out on so that signing in
 *  again returns there. Not from the admin console: the mailed link opens on
 *  the app host, which has none of its routes. */
function loginUrl() {
  const { hostname, pathname } = window.location;
  const next = isAdminHost(hostname) ? null : signInNext(pathname);
  return next ? `${LOGIN_PATH}?${new URLSearchParams({ next })}` : LOGIN_PATH;
}

// The shared client answers a 401 with UnauthorizedError; sending the caller to
// the login page is this app's decision, made here beside the route it names.
//
// Not when already on the login page: assigning the same URL reloads it, the
// reload re-runs this request, and its 401 assigns it again; where nothing can
// mint a session that does not terminate. Nor from a page that needs no
// session: whatever the 401 was, the rest of that page is the caller's to read,
// and a visitor with no way through the login card would simply be stuck.
function request(path: string, opts?: RequestOptions) {
  return sharedRequest(path, opts).catch((e) => {
    if (e instanceof UnauthorizedError && !onLoginPage() && !onPublicPage()) {
      window.location.href = loginUrl();
    }
    throw e;
  });
}

// The map asks from more than one component on the same tick, so the answer is
// held briefly and concurrent askers share one request rather than each making
// it. A failed request is not cached.
const MLAT_VERIFICATION_TTL_MS = 5000;
let mlatCache: any = null;
let mlatCacheTs = 0;
let mlatInflight: Promise<any> | null = null;

function mlatVerification() {
  const now = Date.now();
  if (mlatCache && now - mlatCacheTs < MLAT_VERIFICATION_TTL_MS) return Promise.resolve(mlatCache);
  if (mlatInflight) return mlatInflight;
  mlatInflight = request("/api/test/mlat-verification")
    .then((data) => {
      mlatCache = data;
      mlatCacheTs = Date.now();
      return data;
    })
    .finally(() => {
      mlatInflight = null;
    });
  return mlatInflight;
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
  // (503) — the only two failures a page may repeat back. `next` is the page
  // the mailed link opens once redeemed; omitted, it opens the console's root.
  requestMagicLink: (email, next: string | null = null) =>
    request("/api/auth/magic-link", {
      method: "POST",
      body: JSON.stringify(next ? { email, next } : { email }),
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

  // Claiming a node. The first three take no session: whoever clicked the link
  // in their mail may have no account yet, which is the point. They go through
  // the wrapper anyway so that a claim page opened by somebody already signed
  // in behaves like every other page.
  claimPreview: (token) => request(`/api/auth/claim/${encodeURIComponent(token)}`),
  consumeClaim: (token) =>
    request("/api/auth/claim/consume", { method: "POST", body: JSON.stringify({ token }) }),
  declineClaim: (token) =>
    request("/api/auth/claim/decline", { method: "POST", body: JSON.stringify({ token }) }),
  releaseNode: (nodeId) =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/claim`, { method: "DELETE" }),

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

  // Leaderboard (user-facing)
  leaderboard: () => request("/api/admin/leaderboard"),

  // Admin
  adminUsers: () => request("/api/admin/users"),
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
  mlatVerification,
  mlatAccuracy: () => request("/api/test/mlat-accuracy"),
  // Per-solve history for one MLAT marker (mn<sha256[:10]> hex): the raw solves
  // behind it over the last ~30 min, plus gate rejections near its position.
  mlatHistory: (hex: string) =>
    request(`/api/test/mlat-history?hex=${encodeURIComponent(hex)}`),

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
