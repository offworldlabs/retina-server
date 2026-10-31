import { HttpError } from "@retina/shared";
import { api } from "../../../api/client";
import type { OwnedNode, OwnerSummary } from "../../../types";
import { isAdminHost } from "../../../utils/surface";

/** The node a page is about, as one of two readers sees it. Its owner reads it
 *  from the owner routes: the true geometry, the RF configuration and the
 *  unfuzzed figures, a private node included. Anyone else reads what the
 *  public routes publish. A private node that has left its owner's account
 *  has nothing left to read. */
export type NodeView = OwnerView | PublicView | GoneView;

export interface OwnerView {
  kind: "owner";
  /** The node of record, GET /api/auth/me/nodes/{node_id}. */
  node: OwnedNode;
  /** Null while the server has no analytics for it yet. */
  analytics: OwnerSummary | null;
  /** Why the analytics could not be read. The rest of the owner's page does
   *  not depend on them, so this costs the figures alone. */
  analyticsError: Error | null;
}

export interface PublicView {
  kind: "public";
  /** /api/radar/analytics/{ref}, as published. */
  analytics: any;
  /** The node's entry in /api/radar/nodes, which omits a private node. */
  nodeInfo: any | null;
  /** Set when the owner's list failed in a way that does not say the node is
   *  not theirs, so the page can say it may be missing what its owner sees. */
  ownershipUnknown: Error | null;
}

/** A node that was the caller's and private, and is no longer on their
 *  account. The public routes answer anyone 404 for a private node, so there
 *  is nothing to ask them, and its ref is not sent to them to find that out. */
export interface GoneView {
  kind: "gone";
}

const GONE: GoneView = { kind: "gone" };

function asError(reason: unknown): Error {
  return reason instanceof Error ? reason : new Error(String(reason));
}

function isNotFound(reason: unknown): boolean {
  return reason instanceof HttpError && reason.status === 404;
}

/** Whether a failed owner's list still answers the question it was asked for:
 *  signed out, or no list for this caller, both mean the node is not theirs.
 *  A timeout, a 5xx or a 429 says nothing either way. */
function settlesOwnership(reason: unknown): boolean {
  return reason instanceof HttpError && [401, 403, 404].includes(reason.status);
}

/** The node's entry in the public listing, read as a dictionary through its
 *  own keys only, so a route parameter such as "constructor" finds nothing. */
function listed(listing: any, param: string): any | null {
  const nodes = listing?.nodes;
  if (!nodes || typeof nodes !== "object") return null;
  return Object.prototype.hasOwnProperty.call(nodes, param) ? (nodes[param] ?? null) : null;
}

/**
 * The page's one read. The route parameter is a public identity, a node_ref or
 * a synthetic node's own id, and every owner route takes the node_id. The
 * owner's list is the one place both appear together, so it is matched on the
 * ref first; node_id covers the synthetic case, where the two are equal.
 *
 * A private node's ref is never sent to a public route while it may be the
 * caller's: not when the list says it is theirs, not when the list failed and
 * cannot say, and not once it has left the account. `wasPrivate` is what the
 * page last showed: the node as the caller's and private. Only the page knows
 * that once the list no longer names the node.
 */
export async function loadNode(param: string, wasPrivate = false): Promise<NodeView> {
  // The admin console signs in through Access and has no owner routes, so it
  // never asks them, whoever its session belongs to.
  if (isAdminHost(window.location.hostname)) return loadPublic(param);

  // Fails soft: a signed-out viewer 401s, and anybody who owns nothing here
  // reads the public page. The failure is kept for the public read to weigh.
  const list = await api.myNodes().then(
    (nodes) => ({ nodes: Array.isArray(nodes) ? nodes : [], failure: null as unknown }),
    (failure: unknown) => ({ nodes: [] as OwnedNode[], failure }),
  );
  const owned = list.nodes.find((n) => (n.node_ref && n.node_ref === param) || n.node_id === param);
  if (owned) {
    const view = await loadOwned(owned.node_id);
    if (view) return view;
    // Gone from the account since the list was read.
    return owned.location_private || wasPrivate ? GONE : loadPublic(param);
  }
  if (wasPrivate) {
    // The list answered without it, so it has left the account. A failed list
    // says nothing, and the page keeps what it had under the failure.
    if (list.failure == null) return GONE;
    throw asError(list.failure);
  }
  if (list.failure != null && !settlesOwnership(list.failure)) return loadPublicIfListed(param, list.failure);
  return loadPublic(param);
}

/** The owner's own reading of their node, or null once it has left their
 *  account: the owner routes answer 404 for a node handed back or reassigned
 *  since the list was read. */
async function loadOwned(nodeId: string): Promise<OwnerView | null> {
  // Asked for alongside the node, and settled either way so neither a refusal
  // here nor one of the node's own goes unhandled.
  const analytics = api.myNodeAnalytics(nodeId).then(
    (summary) => ({ summary, error: null as Error | null }),
    (reason: unknown) => ({ summary: null as OwnerSummary | null, error: asError(reason) }),
  );
  let node: OwnedNode;
  try {
    node = await api.myNode(nodeId);
  } catch (e) {
    if (isNotFound(e)) return null;
    throw e;
  }
  const { summary, error } = await analytics;
  if (isNotFound(error)) return null;
  return { kind: "owner", node, analytics: summary ?? null, analyticsError: error };
}

/** What anyone sees, for a node known not to be the caller's. */
async function loadPublic(param: string): Promise<PublicView> {
  const [analytics, listing] = await Promise.all([
    api.nodeAnalytics(param),
    // Fails soft: a private node is absent from this listing, and the page
    // renders on the analytics alone.
    api.nodes().catch(() => null),
  ]);
  return { kind: "public", analytics, nodeInfo: listed(listing, param), ownershipUnknown: null };
}

/** What anyone sees, for a node that may be the caller's own private one: the
 *  owner's list failed and could not say. The public listing names the public
 *  nodes connected now without being asked about any one, so the ref goes to a
 *  public route only once the listing has shown it public. Otherwise, a public
 *  node offline included, the page shows why it could not tell, and a retry
 *  asks the list again. */
async function loadPublicIfListed(param: string, listFailure: unknown): Promise<PublicView> {
  const unknown = asError(listFailure);
  const nodeInfo = listed(await api.nodes().catch(() => null), param);
  if (!nodeInfo) throw unknown;
  const analytics = await api.nodeAnalytics(param).catch((e: unknown) => {
    // Listed, but no analytics published for it: saying it does not exist
    // could be false for its owner, who may have figures of their own.
    if (isNotFound(e)) throw unknown;
    throw e;
  });
  return { kind: "public", analytics, nodeInfo, ownershipUnknown: unknown };
}
