import type { OwnedNode } from "../../../types";
import { isOnline, shortRef } from "../../../utils/nodes";
import type { NetworkNode } from "./feeds";

/**
 * The node picker's two groups: the owner's nodes, private ones included, by
 * node_id; then the rest of the public listing, by node_ref. An owned node on
 * the listing is offered once, as the owner's.
 */

export interface OwnedChoice {
  kind: "owned";
  /** The option's value: `mine:` and the node_id, which no node_ref starts with. */
  value: string;
  label: string;
  node: OwnedNode;
}

export interface NetworkChoice {
  kind: "network";
  /** The option's value: the node_ref, the value the picker has always held. */
  value: string;
  label: string;
  node: NetworkNode;
}

export type NodeChoice = OwnedChoice | NetworkChoice;

export interface NodeChoices {
  owned: OwnedChoice[];
  network: NetworkChoice[];
}

/** What a node is called where its name is not its ref: the name, else the
 *  ref cut short, as the leaderboard calls it. */
export function nodeLabel(name: string | null, ref: string | null): string {
  return name && name !== ref ? name : shortRef(ref) || "Unnamed node";
}

// Numeric, so "Mast 2" comes before "Mast 10"; and by letters alone, so case
// does not split one name's neighbours apart.
const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

/** By label, then by the identity, so the order and the first node in it are
 *  the same on every answer whatever order the server listed them in. */
function sorted<T extends { label: string; tie: string }>(items: T[]): T[] {
  return [...items].sort((a, b) => collator.compare(a.label, b.label) || (a.tie < b.tie ? -1 : a.tie > b.tie ? 1 : 0));
}

/** Two nodes of one group under one label would be one option twice over: the
 *  later ones take their short ref, or a count where they have none. */
function distinct<T extends { label: string; ref: string | null }>(items: T[]): T[] {
  const seen = new Map<string, number>();
  for (const item of items) seen.set(item.label, (seen.get(item.label) ?? 0) + 1);
  const used = new Map<string, number>();
  return items.map((item) => {
    if ((seen.get(item.label) ?? 0) < 2) return item;
    const n = (used.get(item.label) ?? 0) + 1;
    used.set(item.label, n);
    const ref = shortRef(item.ref);
    return { ...item, label: ref && ref !== item.label ? `${item.label} · ${ref}` : `${item.label} (${n})` };
  });
}

/** An option holds text only, so a private node says so in its own label: the
 *  open list shows no badge. */
function ownedLabel(node: OwnedNode): string {
  const label = nodeLabel(node.name, node.node_ref);
  return node.location_private ? `${label} · private` : label;
}

export function nodeChoices(owned: OwnedNode[], network: NetworkNode[], ownedRefs: ReadonlySet<string>): NodeChoices {
  const mine = distinct(
    sorted(
      owned.map((node) => ({
        kind: "owned" as const,
        value: `mine:${node.node_id}`,
        label: ownedLabel(node),
        tie: node.node_id,
        ref: node.node_ref,
        node,
      })),
    ),
  );
  const theirs = distinct(
    sorted(
      network
        .filter((node) => !ownedRefs.has(node.ref))
        .map((node) => ({
          kind: "network" as const,
          value: node.ref,
          label: nodeLabel(node.name, node.ref),
          tie: node.ref,
          ref: node.ref,
          node,
        })),
    ),
  );
  return {
    owned: mine.map(({ kind, value, label, node }) => ({ kind, value, label, node })),
    network: theirs.map(({ kind, value, label, node }) => ({ kind, value, label, node })),
  };
}

/** The choice with this option value, while it is still offered. */
export function findChoice(choices: NodeChoices, value: string | null): NodeChoice | null {
  if (value === null) return null;
  return choices.owned.find((c) => c.value === value) ?? choices.network.find((c) => c.value === value) ?? null;
}

/** Where the page opens: the owner's first node that is online, else their
 *  first, else the network's first. */
export function defaultChoice(choices: NodeChoices): NodeChoice | null {
  return choices.owned.find((c) => isOnline(c.node.status)) ?? choices.owned[0] ?? choices.network[0] ?? null;
}
