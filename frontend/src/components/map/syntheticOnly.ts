// The public demo's fleet filter, pure so it can be unit-tested without
// mounting the aircraft hook.
//
// A demo surface (hidesRealNodes, utils/domains.ts) reads the unfiltered feed
// and takes the real fleet off client-side. Dropping whole entries does not
// finish the job, because an entry can name both fleets at once: the backend
// hands every track within 8 km of an active simulated trail the same
// ground_truth_hex (services/track_gates.py, resolve_ground_truth_hex, which
// ignores altitude), and dedup_aircraft then folds those entries onto one
// winner and rebuilds its contributing list as the union of every folded
// member's nodes. So an entry kept for its synthetic contributor can still
// carry a real node's ref inside it, and each kept entry is scrubbed as well
// as filtered.

import { isSyntheticNode } from "../../utils/nodeKind";

// A feed entry carries no is_synthetic flag of its own, so the ref is all there
// is to go on: isSyntheticNode falls back to the prefix, which is what still
// separates a synthetic id from a published nde… ref.
export const isSyntheticRef = (ref: string): boolean => isSyntheticNode({}, ref);

/** The node-attributed fields of an aircraft or detection-arc feed entry. */
export interface NodeAttributed {
  node_ref?: string;
  contributing_node_refs?: string[];
  n_nodes?: number;
}

/**
 * Whether an entry is attributed to the synthetic fleet: node_ref for a
 * single-node track, contributing_node_refs for a solve. Mirrors the server's
 * own real-only filter (services/tasks/aircraft_flush.py,
 * filter_payload_to_nodes), so an entry naming no node at all belongs to
 * neither fleet and is dropped rather than kept by default.
 */
export function fromSyntheticNode(entry: NodeAttributed): boolean {
  if (entry.node_ref && isSyntheticRef(entry.node_ref)) return true;
  const contributors = entry.contributing_node_refs;
  return Array.isArray(contributors) && contributors.some(isSyntheticRef);
}

/**
 * A kept entry with every real node ref taken out of it.
 *
 * Both node-attributed fields are rendered verbatim on this surface (the
 * detail panel prints the claiming node and the detecting list by name) and
 * both feed the detection oracle, so a mixed entry has to be cleaned rather
 * than merely kept.
 */
export function scrubToSyntheticNodes<T extends NodeAttributed>(entry: T): T {
  const refs = entry.contributing_node_refs;
  const kept = Array.isArray(refs) ? refs.filter(isSyntheticRef) : null;
  const dropsClaimant = typeof entry.node_ref === "string" && !isSyntheticRef(entry.node_ref);
  const dropsContributors = kept !== null && kept.length !== refs.length;
  if (!dropsClaimant && !dropsContributors) return entry;

  const patch: NodeAttributed = {};
  // Absent rather than redacted: the feed already publishes a null node_ref for
  // a solve with no single claimant, so every consumer (arcBuffer,
  // updateDetections, the detail panel) already handles the field being empty.
  // The arc such an entry carries goes with it, which is the point: it is the
  // real node's measurement.
  if (dropsClaimant) patch.node_ref = undefined;
  if (dropsContributors) {
    patch.contributing_node_refs = kept;
    // n_nodes describes the contributing list, so it must describe the list as
    // shown; the real solve's size is part of the membership this surface
    // withholds. Left alone on an entry nothing was removed from, where it
    // keeps the backend's meaning.
    if (typeof entry.n_nodes === "number") patch.n_nodes = kept.length;
  }
  // Copied, not edited in place: the caller still holds the parsed feed payload.
  return { ...entry, ...patch } as T;
}

/** The detecting_nodes map (hex → refs) with the real fleet taken out. */
export function syntheticDetectingNodes(
  detecting: Record<string, string[]> | undefined | null,
): Record<string, string[]> {
  return Object.fromEntries(
    Object.entries(detecting || {}).map(([hex, refs]) => [
      hex,
      (Array.isArray(refs) ? refs : []).filter(isSyntheticRef),
    ]),
  );
}
