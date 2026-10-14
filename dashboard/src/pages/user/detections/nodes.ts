/**
 * The node side of the Detections page: which nodes are behind an aircraft,
 * what each is called, and what the node filter offers. Both feeds key a node
 * by its node_ref, never its node_id, so a ref is the one key used here.
 */

import type { FeedAircraft, OwnedNode } from "../../../types";
import { shortRef } from "../../../utils/nodes";

type Contributors = Pick<FeedAircraft, "node_ref" | "contributing_node_refs">;

/** The nodes behind one aircraft: the solve's contributors and the node that
 *  reported it, each once. A single-node entry has only its node_ref; a
 *  multinode solve, or a transponder several nodes claim, lists its nodes in
 *  contributing_node_refs and may carry no node_ref at all. */
export function nodesBehind(ac: Contributors): string[] {
  const refs = Array.isArray(ac.contributing_node_refs) ? ac.contributing_node_refs : [];
  return [...new Set([ac.node_ref, ...refs].filter((ref): ref is string => typeof ref === "string" && ref !== ""))];
}

/** Whether `ref` is one of the nodes behind an aircraft. */
export function seenByNode(ac: Contributors, ref: string): boolean {
  return ac.node_ref === ref || (Array.isArray(ac.contributing_node_refs) && ac.contributing_node_refs.includes(ref));
}

/** What a node is called on this page. A ref stands in where there is no
 *  name, cut to the part that tells it apart, and is set as an id is. */
export interface NodeLabel {
  text: string;
  isRef: boolean;
}

/** A node whose name is only its ref goes by the short ref, as the
 *  leaderboard names it. */
function labelOf(ref: string, name: string | null | undefined): NodeLabel {
  return name && name !== ref ? { text: name, isRef: false } : { text: shortRef(ref), isRef: true };
}

/** Names each node by the owner's own list first, which carries the label
 *  they gave it and is never published, then by the public listing. A node in
 *  neither, such as one that joined since the listing was read, goes by its ref. */
export function nodeNamer(owned: readonly OwnedNode[], listed: ReadonlyMap<string, string | null>) {
  const names = new Map(listed);
  for (const node of owned) {
    if (node.node_ref) names.set(node.node_ref, node.name);
  }
  return (ref: string): NodeLabel => labelOf(ref, names.get(ref));
}

export type NameOf = ReturnType<typeof nodeNamer>;

/** The names /api/radar/nodes publishes, keyed by node_ref. */
export function listedNames(listing: unknown): Map<string, string | null> {
  const nodes = (listing as { nodes?: Record<string, { name?: unknown } | null> } | null)?.nodes;
  const names = new Map<string, string | null>();
  if (!nodes || typeof nodes !== "object") return names;
  for (const [ref, entry] of Object.entries(nodes)) {
    names.set(ref, typeof entry?.name === "string" ? entry.name : null);
  }
  return names;
}

/** One choice in the node filter. */
export interface FilterOption {
  ref: string;
  text: string;
}

const byText = (a: FilterOption, b: FilterOption) =>
  a.text.localeCompare(b.text, undefined, { numeric: true, sensitivity: "base" });

/** The options for a run of nodes, sorted by name. Two nodes may share a name,
 *  an owner's label or a node's own, so each of those also carries its ref. */
export function filterOptions(refs: Iterable<string>, nameOf: NameOf): FilterOption[] {
  const labels = [...new Set(refs)].map((ref) => ({ ref, label: nameOf(ref) }));
  const seen = new Map<string, number>();
  for (const { label } of labels) seen.set(label.text, (seen.get(label.text) ?? 0) + 1);
  return labels
    .map(({ ref, label }) => ({
      ref,
      text: !label.isRef && (seen.get(label.text) ?? 0) > 1 ? `${label.text} (${shortRef(ref)})` : label.text,
    }))
    .sort(byText);
}
