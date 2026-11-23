import { UnauthorizedError, request as sharedRequest, type CurrentUser, type RequestOptions } from "@retina/shared";

import type { AdsbResponse, AvailabilityResponse, DetectionsResponse, TracksResponse } from "../pages/map/replay/types";
import { isPublicRoute } from "../utils/publicRoutes";
import { signInNext } from "../utils/signInNext";
import { isAdminHost } from "../utils/surface";
import type {
  AccountDeleted,
  AccountSession,
  AdminAccount,
  AdminClaim,
  AdminEvent,
  AdminFleet,
  AdminUserDetail,
  AdminUserPage,
  AircraftFrame,
  ArchiveListing,
  ClaimMailSent,
  ConfigVersion,
  ConnectionCheck,
  EmailChangeSent,
  Leaderboard,
  MyClaim,
  NodeReport,
  OwnedNode,
  OwnerAnalytics,
  OwnerEvent,
  OwnerNodePatch,
  OwnerSummary,
  PolledRadarListing,
  PolledRadarProbe,
  PolledRadarTrust,
  Publication,
  SnrSeries,
} from "../types";

/** Must match the route in App.tsx. */
const LOGIN_PATH = "/login";

/** nginx's read timeout for /api/auth/me, which a probe must finish inside. */
const RADAR_PROBE_TIMEOUT_MS = 30_000;

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

/** The owner's routes, every one behind the session, so a 401 from one says the
 *  session is gone. Not /api/auth/me itself: the shared useCurrentUser asks it
 *  around this wrapper, and a 401 there is the answer it settles on. */
const OWNER_ROUTES = "/api/auth/me/";

const sessionEndedListeners = new Set<() => void>();

/** Calls `listener` when an owner route answers 401 on a page the wrapper below
 *  leaves the caller on: the session ended elsewhere (signed out in another
 *  tab, expired, revoked). Returns the unsubscribe. */
export function onSessionEnded(listener: () => void): () => void {
  sessionEndedListeners.add(listener);
  return () => {
    sessionEndedListeners.delete(listener);
  };
}

/** The sessions this document has adopted in place, counted. A mailed link
 *  redeemed without a page load replaces the session the requests below were
 *  sent under, so each one is sent under the count of its time. */
let session = 0;

/** Counts a session the document has just adopted, and returns its number. */
export function adoptSession(): number {
  session += 1;
  return session;
}

/** The number of the session requests go out under now. */
export function currentSession(): number {
  return session;
}

// The shared client answers a 401 with UnauthorizedError; sending the caller to
// the login page is this app's decision, made here beside the route it names.
//
// Not when already on the login page: assigning the same URL reloads it, the
// reload re-runs this request, and its 401 assigns it again; where nothing can
// mint a session that does not terminate. Nor from a page that needs no
// session: whatever the 401 was, the rest of that page is the caller's to read,
// and a visitor with no way through the login card would simply be stuck.
//
// Staying put, an owner route's 401 still ends the session here, in place: the
// page would otherwise go on showing what the session fetched (a private node's
// files, its true position) to whoever now holds the screen. Not where the
// caller is sent away, which drops everything with the page, and where the
// guard's own redirect would race the load.
//
// Neither for a request sent under a session this document has since replaced.
// It can answer 401 once the server has dropped that session, and the answer
// is about that one: acted on, it would end, or send away, the session adopted
// after it.
function request(path: string, opts?: RequestOptions) {
  const sentUnder = session;
  return sharedRequest(path, opts).catch((e) => {
    if (e instanceof UnauthorizedError && sentUnder === session) {
      if (!onLoginPage() && !onPublicPage()) window.location.href = loginUrl();
      else if (path.startsWith(OWNER_ROUTES)) for (const listener of sessionEndedListeners) listener();
    }
    throw e;
  });
}

/** The path of one file in the owner's archive. The key is server-issued and
 *  appended as it came, as the public archive's is. */
export function ownerArchivePath(key: string) {
  return `/api/auth/me/archive/${key}`;
}

export const api = {
  // Auth. Who the caller is comes from the shared useCurrentUser, which goes
  // straight to the shared client: a 401 there is the answer it wants, and
  // RequireAuth routes on it without the full page load this wrapper costs.
  logout: () => request("/api/auth/logout", { method: "POST" }),

  // Magic-link sign-in. These answer before there is a session to lose, so the
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
  // Resolves {email} without spending the token; rejects 404 for unknown,
  // expired and already-redeemed alike.
  previewMagicLink: (token) => request(`/api/auth/magic-link/${encodeURIComponent(token)}`),
  // Resolves {user} and leaves the session cookie behind it; rejects 400 with
  // one detail for unknown, expired and already-redeemed alike.
  consumeMagicLink: (token) =>
    request("/api/auth/magic-link/consume", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),

  // The signed-in account. `me` reads it again for a page already open, as
  // the account page does; unlike the shared useCurrentUser's probe it goes
  // through the wrapper above, so a 401 ends the session as an owner route's
  // does. updateMe resolves the user as /api/auth/me answers it; a name the
  // server refuses (empty once trimmed, over 64 characters, or holding
  // control characters) rejects 422 with the reason as its detail.
  me: (): Promise<CurrentUser> => request("/api/auth/me"),
  updateMe: (patch: { name: string }): Promise<CurrentUser> =>
    request("/api/auth/me", { method: "PATCH", body: JSON.stringify(patch) }),
  // Resolves once every node the account owned is released, every session has
  // ended and the account is gone, with the session cookie cleared. Rejects 400
  // when `email`, trimmed and in any case, is not the account's address.
  deleteAccount: (email: string): Promise<AccountDeleted> =>
    request("/api/auth/me/delete", { method: "POST", body: JSON.stringify({ email }) }),
  // The account's signed-in browsers, the most recently used first.
  mySessions: (): Promise<AccountSession[]> => request("/api/auth/me/sessions"),
  // Rejects 404 for a session that is not the account's or has ended already.
  // Ending the current one is signing this browser out.
  revokeSession: (sessionId: string): Promise<{ ok: true }> =>
    request(`/api/auth/me/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" }),
  // Every session but the current one; resolves how many ended.
  revokeOtherSessions: (): Promise<{ revoked: number }> =>
    request("/api/auth/me/sessions/revoke-others", { method: "POST" }),

  // Moving the account to another sign-in address. The request mails a link to
  // the new address and resolves 202 with it normalised; the current address
  // signs in until the link is opened, and a later request kills the earlier
  // link. Rejects 400 for an invalid address or the current one, and 429 or
  // 503 as a sign-in link does. An address another account holds or once held
  // answers like a free one.
  requestEmailChange: (email: string): Promise<EmailChangeSent> =>
    request("/api/auth/me/email", { method: "POST", body: JSON.stringify({ email }) }),
  // These two take no session: the token in the mailed link is the proof. The
  // preview resolves {email} without spending it, and rejects 404 once it is no
  // longer valid. Consuming resolves {email} and opens no session; it rejects
  // 400 for a link no longer valid and 409 for an address taken meanwhile.
  emailChangePreview: (token: string): Promise<{ email: string }> =>
    request(`/api/auth/email-change/${encodeURIComponent(token)}`),
  consumeEmailChange: (token: string): Promise<{ email: string }> =>
    request("/api/auth/email-change/consume", { method: "POST", body: JSON.stringify({ token }) }),

  // Self-service node ownership. Each route below is the caller's own nodes
  // only: a node they do not own and one that does not exist both reject 404
  // with the same body.
  myNodes: (): Promise<OwnedNode[]> => request("/api/auth/me/nodes"),
  myNode: (nodeId: string): Promise<OwnedNode> =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}`),
  // Resolves the node after the change. A null or empty name clears the label;
  // a name the server refuses rejects 422 with the reason as its detail.
  updateMyNode: (nodeId: string, patch: OwnerNodePatch): Promise<OwnedNode> =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  myAircraft: (): Promise<AircraftFrame> => request("/api/auth/me/aircraft"),
  // Unfuzzed analytics, private nodes included: all of them keyed by node_ref,
  // or one node's. The one node resolves null while it has none yet.
  myAnalytics: (): Promise<OwnerAnalytics> => request("/api/auth/me/analytics"),
  myNodeAnalytics: (nodeId: string): Promise<OwnerSummary | null> =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/analytics`),
  // The last heartbeat; null for a node that has never sent one.
  myNodeReport: (nodeId: string): Promise<NodeReport | null> =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/report`),
  // Newest first. The server takes 1 to 200 versions and 1 to 500 events.
  myNodeConfigHistory: (nodeId: string, limit = 50): Promise<{ versions: ConfigVersion[] }> =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/config-history?limit=${limit}`),
  myNodeEvents: (nodeId: string, limit = 100): Promise<{ events: OwnerEvent[] }> =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/events?limit=${limit}`),
  // Five-minute buckets over the last `hours`, which the server takes as 1 to 72.
  myNodeSnr: (nodeId: string, hours = 24): Promise<SnrSeries> =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/snr?hours=${hours}`),
  // The public archive's listing, over the caller's nodes only and private
  // ones included. A node_ref that is not theirs lists nothing. Params left
  // out are not sent.
  myArchive: (
    params: { date?: string; nodeRef?: string | null; limit?: number; offset?: number },
    opts?: RequestOptions,
  ): Promise<ArchiveListing> => {
    const query = new URLSearchParams();
    if (params.date) query.set("date", params.date);
    if (params.nodeRef) query.set("node_ref", params.nodeRef);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const qs = query.toString();
    return request(`/api/auth/me/archive${qs ? `?${qs}` : ""}`, opts);
  },
  // One archive file, the body the public archive sends; 404 for a key that is
  // not one of the caller's nodes.
  myArchiveFile: (key: string, opts?: RequestOptions): Promise<unknown> => request(ownerArchivePath(key), opts),

  // Registering a stock blah2 radar. Each call probes the radar, which can take
  // a radar's worth of seconds, so each outwaits the default timeout. A refusal
  // is an HttpError whose body carries a `code`, and for `config_changed` the
  // radar's new `probe`.
  probePolledRadar: (address: string): Promise<PolledRadarProbe> =>
    request("/api/auth/me/polled-radars/probe", {
      method: "POST",
      body: JSON.stringify({ address }),
      timeoutMs: RADAR_PROBE_TIMEOUT_MS,
    }),
  registerPolledRadar: (registration: {
    address: string;
    fingerprint: string;
    publication: Publication;
  }): Promise<{ node_id: string; epoch: number; trust_state: PolledRadarTrust }> =>
    request("/api/auth/me/polled-radars", {
      method: "POST",
      body: JSON.stringify(registration),
      timeoutMs: RADAR_PROBE_TIMEOUT_MS,
    }),
  // Moving an owner's radar is the same two steps, addressed by its node_id.
  // Removing one is releaseNode, which retires a polled radar.
  probePolledRadarAddress: (nodeId: string, address: string): Promise<PolledRadarProbe> =>
    request(`/api/auth/me/polled-radars/${encodeURIComponent(nodeId)}/probe`, {
      method: "POST",
      body: JSON.stringify({ address }),
      timeoutMs: RADAR_PROBE_TIMEOUT_MS,
    }),
  movePolledRadar: (
    nodeId: string,
    move: { address: string; fingerprint: string },
  ): Promise<{ node_id: string; epoch: number; trust_state: PolledRadarTrust }> =>
    request(`/api/auth/me/polled-radars/${encodeURIComponent(nodeId)}/address`, {
      method: "PUT",
      body: JSON.stringify(move),
      timeoutMs: RADAR_PROBE_TIMEOUT_MS,
    }),

  // Claiming a node. The first three take no session: whoever clicked the link
  // in their mail may have no account yet, which is the point. They go through
  // the wrapper anyway so that a claim page opened by somebody already signed
  // in behaves like every other page.
  claimPreview: (token) => request(`/api/auth/claim/${encodeURIComponent(token)}`),
  consumeClaim: (token) =>
    request("/api/auth/claim/consume", { method: "POST", body: JSON.stringify({ token }) }),
  declineClaim: (token) =>
    request("/api/auth/claim/decline", { method: "POST", body: JSON.stringify({ token }) }),
  // The three resends below follow the node's own resend, rules and limits
  // alike: 409 for a node owned by then, 429 and 503 as that one answers, and
  // never resolved unless a mail was queued. By the claim link's token, which
  // is the proof, so no session; 404 once the token is not the node's current
  // challenge, or its address cannot be mailed.
  resendClaimByToken: (token: string): Promise<ClaimMailSent> =>
    request("/api/auth/claim/resend", { method: "POST", body: JSON.stringify({ token }) }),
  // Open claims on nodes given one of the account's addresses, current or retired.
  myClaims: (): Promise<MyClaim[]> => request("/api/auth/me/claims"),
  // 404 for a node that is not an open claim of the account's.
  resendMyClaim: (nodeRef: string): Promise<ClaimMailSent> =>
    request(`/api/auth/me/claims/${encodeURIComponent(nodeRef)}/resend`, { method: "POST" }),
  releaseNode: (nodeId: string): Promise<{ ok: true }> =>
    request(`/api/auth/me/nodes/${encodeURIComponent(nodeId)}/claim`, { method: "DELETE" }),

  // Radar / nodes
  nodes: () => request("/api/radar/nodes"),
  status: () => request("/api/radar/status"),
  analytics: () => request("/api/radar/analytics"),
  nodeAnalytics: (id) => request(`/api/radar/analytics/${id}`),
  aircraft: () => request("/api/radar/data/aircraft.json"),
  overlaps: () => request("/api/radar/association/overlaps"),
  anomalies: () => request("/api/radar/anomalies"),

  // Archive
  archive: (limit = 50, offset = 0, nodeRef = null) => {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (nodeRef) params.set("node_ref", nodeRef);
    return request(`/api/data/archive?${params}`);
  },

  // The archive explorer's published solves, and how many there are per hour.
  // Both routes are public, so they go straight to the shared client: a 401
  // from one says nothing about a session, and must not send a visitor on the
  // map to the login page. Each sends the origin alone as its Referer, as
  // every public archive request does. A tracks window is epoch ms, sent as
  // integers, and spans at most 60 minutes; availability takes UTC days,
  // inclusive, at most 92 of them.
  archiveTracks: (
    params: { from: number; to: number; fleet?: "real" },
    opts?: RequestOptions,
  ): Promise<TracksResponse> => {
    const query = new URLSearchParams({
      from: String(Math.floor(params.from)),
      to: String(Math.floor(params.to)),
      fleet: params.fleet ?? "real",
    });
    return sharedRequest(`/api/data/tracks?${query}`, {
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 30_000,
      referrerPolicy: "strict-origin",
    });
  },
  trackAvailability: (
    params: { from: string; to: string; fleet?: "real" },
    opts?: RequestOptions,
  ): Promise<AvailabilityResponse> => {
    const query = new URLSearchParams({ from: params.from, to: params.to, fleet: params.fleet ?? "real" });
    return sharedRequest(`/api/data/tracks/availability?${query}`, {
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 15_000,
      referrerPolicy: "strict-origin",
    });
  },
  // The detections the nodes published in a window of at most 10 minutes,
  // which archive replay draws as arcs. Public like the tracks, and sent the
  // same way: epoch ms as integers, the origin alone as the Referer. `bbox`
  // is [south, west, north, east]; the map never sends one.
  archiveDetections: (
    params: { from: number; to: number; fleet?: "real"; bbox?: readonly [number, number, number, number] },
    opts?: RequestOptions,
  ): Promise<DetectionsResponse> => {
    const query = new URLSearchParams({
      from: String(Math.floor(params.from)),
      to: String(Math.floor(params.to)),
      fleet: params.fleet ?? "real",
    });
    if (params.bbox) query.set("bbox", params.bbox.join(","));
    return sharedRequest(`/api/data/detections?${query}`, {
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 30_000,
      referrerPolicy: "strict-origin",
    });
  },
  // The ADS-B traffic the server knew of in a window of at most 60 minutes,
  // one fix per aircraft per 10 s snapshot, which archive replay shows as the
  // ground-truth layer. Public, and sent as the tracks are. `bbox` is
  // [south, west, north, east]; the map never sends one.
  archiveAdsb: (
    params: { from: number; to: number; fleet?: "real"; bbox?: readonly [number, number, number, number] },
    opts?: RequestOptions,
  ): Promise<AdsbResponse> => {
    const query = new URLSearchParams({
      from: String(Math.floor(params.from)),
      to: String(Math.floor(params.to)),
      fleet: params.fleet ?? "real",
    });
    if (params.bbox) query.set("bbox", params.bbox.join(","));
    return sharedRequest(`/api/data/adsb?${query}`, {
      ...opts,
      timeoutMs: opts?.timeoutMs ?? 30_000,
      referrerPolicy: "strict-origin",
    });
  },

  // Custody
  custody: () => request("/api/custody/status"),

  // Test dashboard (fleet overview)
  fleetDashboard: () => request("/api/test/dashboard"),

  // Leaderboard (user-facing)
  leaderboard: (): Promise<Leaderboard> => request("/api/radar/leaderboard"),

  // Admin: accounts. A page of them newest first, `q` keeping the addresses
  // that contain it in any case; the server takes 1 to 200 a page.
  adminUsers: (params: { q?: string; limit?: number; offset?: number } = {}): Promise<AdminUserPage> => {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const qs = query.toString();
    return request(`/api/admin/users${qs ? `?${qs}` : ""}`);
  },
  // Each of these rejects 404 "User not found" for an id that is no account's.
  adminUser: (userId: string): Promise<AdminUserDetail> =>
    request(`/api/admin/users/${encodeURIComponent(userId)}`),
  // Deactivating ends every session the account holds.
  adminDeactivateUser: (userId: string): Promise<AdminAccount> =>
    request(`/api/admin/users/${encodeURIComponent(userId)}/deactivate`, { method: "POST" }),
  adminReactivateUser: (userId: string): Promise<AdminAccount> =>
    request(`/api/admin/users/${encodeURIComponent(userId)}/reactivate`, { method: "POST" }),
  // Resolves once the mail is queued. Refused 409 for a deactivated account,
  // 429 while it holds too many links and 503 when no mail can leave, each
  // with a detail that says so.
  adminSendSignInLink: (userId: string): Promise<{ status: "queued"; email: string }> =>
    request(`/api/admin/users/${encodeURIComponent(userId)}/sign-in-link`, { method: "POST" }),
  // The account's own deletion, done for it: every node it owned released,
  // every session ended and the account gone; resolves how many nodes were
  // released. Rejects 409, with the account still there and signing in, when
  // a node was claimed for it while it was being deleted, which a second try
  // finishes.
  adminDeleteUser: (userId: string): Promise<AccountDeleted> =>
    request(`/api/admin/users/${encodeURIComponent(userId)}`, { method: "DELETE" }),
  // For an owner who has lost their address: the owner's own address change,
  // started for them. The same confirmation goes to the new address; 404 for
  // an unknown user, 409 for an address another account holds or a deactivated
  // account, and 400, 429 and 503 as requestEmailChange.
  adminChangeUserEmail: (userId: string, email: string): Promise<Pick<EmailChangeSent, "status" | "email">> =>
    request(`/api/admin/users/${encodeURIComponent(userId)}/email`, {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  // The log, newest first; with a user id, only the events about that account.
  adminEvents: (limit = 200, userId: string | null = null): Promise<AdminEvent[]> => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (userId) query.set("user_id", userId);
    return request(`/api/admin/events?${query}`);
  },
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
  // One row per node across the registry and the Mender mirror, with how old
  // the mirror is.
  adminFleet: (): Promise<AdminFleet> => request("/api/admin/fleet"),

  // MLAT verification — aggregated solver-vs-truth stats
  mlatVerification: () => request("/api/test/mlat-verification"),
  mlatAccuracy: () => request("/api/test/mlat-accuracy"),
  // Per-solve history for one MLAT marker (mn<sha256[:10]> hex): the raw solves
  // behind it over the last ~30 min, plus gate rejections near its position.
  mlatHistory: (hex: string) =>
    request(`/api/test/mlat-history?hex=${encodeURIComponent(hex)}`),

  // Admin: node identity. {node_ref: node_id} for the fleet — the one route
  // that crosses the publication boundary, which is why it is admin-only.
  adminNodeRefs: () => request("/api/admin/node-refs"),

  // Admin: each node's last heartbeat self-report, a list of rows that each
  // carry their node_id.
  adminNodeReports: () => request("/api/admin/node-reports"),

  // Admin: node ownership
  adminNodeOwners: () => request("/api/admin/node-owners"),
  adminNodeContacts: () => request("/api/admin/node-contacts"),
  // A null user unassigns the node, which a polled radar refuses 409. A node
  // or account that does not exist rejects 404.
  adminSetNodeOwner: (
    nodeId: string,
    userId: string | null,
  ): Promise<{ ok: true; node_id: string; user_id: string | null }> =>
    request(`/api/admin/nodes/${encodeURIComponent(nodeId)}/owner`, {
      method: "PUT",
      body: JSON.stringify({ user_id: userId }),
    }),
  // The owner, the address on file and the claim link out, which a stuck or
  // bounced claim is read from; 404 "Node not found" for an unregistered id.
  adminNodeClaim: (nodeId: string): Promise<AdminClaim> =>
    request(`/api/admin/nodes/${encodeURIComponent(nodeId)}/claim`),

  // Admin: polled radars and the trust decision on each. The epoch is the one
  // the page showed; the server refuses it with a 409 once the radar has moved on.
  adminPolledRadars: (): Promise<PolledRadarListing> => request("/api/admin/polled-radars"),
  setAdminPolledRadarTrust: (nodeId: string, trustState: PolledRadarTrust, epoch: number) =>
    request(`/api/admin/polled-radars/${encodeURIComponent(nodeId)}/trust`, {
      method: "PUT",
      body: JSON.stringify({ trust_state: trustState, epoch }),
    }),
  // Connecting a radar to an operator's address: the owner's two steps, with the address.
  adminCheckConnection: (address: string, email: string): Promise<ConnectionCheck> =>
    request("/api/admin/polled-radars/probe", {
      method: "POST",
      body: JSON.stringify({ address, email }),
      timeoutMs: RADAR_PROBE_TIMEOUT_MS,
    }),
  adminConnectPolledRadar: (connection: {
    address: string;
    fingerprint: string;
    publication: Publication;
    email: string;
  }): Promise<{ node_id: string; epoch: number; trust_state: PolledRadarTrust; owner: { email: string; created: boolean } }> =>
    request("/api/admin/polled-radars", {
      method: "POST",
      body: JSON.stringify(connection),
      timeoutMs: RADAR_PROBE_TIMEOUT_MS,
    }),
  adminWithdrawPolledRadar: (nodeId: string) =>
    request(`/api/admin/polled-radars/${encodeURIComponent(nodeId)}`, { method: "DELETE" }),
};
