/**
 * The node side of the filters: who exists, and which of them the radius
 * admits. Kept apart from `filters.ts`, which asks only whether a file's own
 * fields pass and takes the node set as given.
 */

import type { Scope } from "../../../hooks/useScopeParam";
import type { OwnedNode } from "../../../types";
import { distanceKm } from "../../../utils/geo";
import { isSyntheticNode } from "../../../utils/nodeKind";
import { isNearNode, type NearFilter, type NearPoint } from "./urlState";

/** What a registry's positions are: the published ones, displaced by up to
 *  their uncertainty, or the owner's own nodes' exact ones, which only the
 *  owner is sent. Every node in one registry is one or the other. */
export type Positions = "published" | "exact";

export interface RegistryNode {
  id: string;
  name: string;
  status: string | null;
  synthetic: boolean;
  /** null when the node publishes no position, which a radius filter treats
   *  as disqualifying rather than as the origin. */
  lat: number | null;
  lon: number | null;
  uncertaintyKm: number | null;
}

/** A node seen only in an archive key has no registry entry and so no server
 *  flag; its prefix is the only thing left to classify it by. */
export function isSynthetic(registry: Map<string, RegistryNode>, id: string): boolean {
  const known = registry.get(id);
  return known ? known.synthetic : isSyntheticNode({}, id);
}

/** Whether the fleet has a synthetic node at all, selected or not. Asked of
 *  every known node so it does not flip as the selection narrows; it can
 *  still turn true after first paint as listings discover synthetic ids. */
export function anyNodeSynthetic(
  registry: Map<string, RegistryNode>,
  discovered: Set<string>,
): boolean {
  return knownNodeIds(registry, discovered).some((id) => isSynthetic(registry, id));
}

/** Every node the picker can offer: the registry, plus ids that only ever
 *  appeared in an archive key. Sorted, so the list does not reorder itself as
 *  listings arrive. */
export function knownNodeIds(
  registry: Map<string, RegistryNode>,
  discovered: Set<string>,
): string[] {
  return Array.from(new Set([...registry.keys(), ...discovered])).sort();
}

/**
 * Which nodes the current filters admit.
 *
 * A null selection means every node there is, including ones discovered
 * later; an empty one means none. This answers what to *show*, never what to
 * *fetch*: narrowing here must not reach the listing scope, because a
 * one-node listing cannot answer a request for the whole fleet however few
 * nodes survive the radius.
 */
export function effectiveNodeIds(
  registry: Map<string, RegistryNode>,
  discovered: Set<string>,
  nodeSel: Set<string> | null,
  near: NearPoint | null,
): Set<string> {
  const candidates = nodeSel ?? new Set(knownNodeIds(registry, discovered));
  if (!near) return new Set(candidates);

  const admitted = new Set<string>();
  for (const id of candidates) {
    const node = registry.get(id);
    // No published position is not the origin: a node that cannot be placed
    // cannot be within a radius of anywhere.
    if (!node || node.lat === null || node.lon === null) continue;
    if (distanceKm(near.lat, near.lon, node.lat, node.lon) <= near.km) admitted.add(id);
  }
  return admitted;
}

/** Where a radius filter is measured from: a point where it says, a node
 *  where the registry puts it, and nowhere for a node the registry cannot
 *  place. Only the owner's own registry holds the nodes a centre names, so
 *  that is the one a named centre is placed from. */
export function centreOf(near: NearFilter | null, registry: Map<string, RegistryNode>): NearPoint | null {
  if (!isNearNode(near)) return near;
  const node = registry.get(near.node);
  if (!node || node.lat === null || node.lon === null) return null;
  return { lat: node.lat, lon: node.lon, km: near.km };
}

const NO_REFS: ReadonlySet<string> = new Set();

/** The node_refs of the owner's nodes that keep off the public archive. */
export function privateRefs(owned: readonly OwnedNode[]): Set<string> {
  return new Set(owned.filter((n) => n.location_private && n.node_ref).map((n) => n.node_ref as string));
}

/**
 * Which node ids a scope's archive can list. The owner's holds their own nodes
 * and no other; the network's holds every node but their private ones, whose
 * refs must never reach it. With no owner's list, a visitor's or one that did
 * not load, only `withheld` is known to keep out: the private nodes the page
 * learned of from a list it read before, a previous session's above all.
 */
export function scopeAdmits(
  scope: Scope,
  owned: readonly OwnedNode[],
  withheld: ReadonlySet<string> = NO_REFS,
): (id: string) => boolean {
  if (scope === "mine") {
    const mine = new Set(owned.map((n) => n.node_ref).filter(Boolean));
    return (id) => mine.has(id);
  }
  const hidden = privateRefs(owned);
  return (id) => !hidden.has(id) && !withheld.has(id);
}

/**
 * A node selection carried into a scope that may not hold all of it, the
 * nodes it cannot list dropped. One left with none of its nodes falls back to
 * every node rather than to none: what was chosen is not in this archive, and
 * an empty result would read as no data rather than as no such node. An empty
 * selection asked for none, and stays as it is.
 */
export function selectionIn(
  nodeSel: Set<string> | null,
  admits: (id: string) => boolean,
): Set<string> | null {
  if (nodeSel === null || nodeSel.size === 0) return nodeSel;
  const kept = Array.from(nodeSel).filter(admits);
  if (kept.length === nodeSel.size) return nodeSel;
  return kept.length ? new Set(kept) : null;
}

/** What a node is to the signed-in owner, where that is worth saying: theirs,
 *  among the network's nodes, or private, among their own, where every node
 *  is theirs. */
export type NodeMark = "yours" | "private";

/** The marks a scope draws, by node id. None for a visitor, who owns nothing. */
export function nodeMarks(scope: Scope, owned: readonly OwnedNode[]): Map<string, NodeMark> {
  const marks = new Map<string, NodeMark>();
  for (const node of owned) {
    if (!node.node_ref) continue;
    if (scope === "network") marks.set(node.node_ref, "yours");
    else if (node.location_private) marks.set(node.node_ref, "private");
  }
  return marks;
}
