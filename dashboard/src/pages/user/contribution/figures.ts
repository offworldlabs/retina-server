import type { LeaderboardRow, OwnedNode } from "../../../types";
import { DASH, formatPercent } from "../../../utils/format";
import { detectionCount, shortRef } from "../../../utils/nodes";

/**
 * The Contribution page's arithmetic, kept apart from its markup so each
 * reading has one definition: the network total, a share of it, a standing on
 * the leaderboard and the overlaps a node is part of.
 */

/** The parts of a per-node analytics summary the page reads. The public map
 *  and the owner's own carry the same keys. */
export interface NodeSummary {
  metrics?: { total_detections?: number | null; availability_7d?: number | null; [key: string]: unknown } | null;
  detection_area?: { n_detections?: number | null; [key: string]: unknown } | null;
  trust?: { trust_score?: number | null; [key: string]: unknown } | null;
}

/**
 * Every node in the network once, keyed by node_ref.
 *
 * The public map is the same for every caller and leaves a private node out,
 * its owner included; the owner's map (/api/auth/me/analytics) carries the
 * true figures of all their nodes under their refs. Where a ref is in both,
 * the owner's entry replaces the public one rather than standing beside it:
 *
 *   network total = sum over refs in (public ∪ owner) of detections(owner[ref] ?? public[ref])
 *   share         = detections(owner[ref]) / network total
 *
 * so no node is counted twice, and the shares of every node add up to 100%.
 */
export function networkNodes(
  published: Record<string, NodeSummary> | null | undefined,
  own: ReadonlyMap<string, NodeSummary> | null,
): Map<string, NodeSummary> {
  const nodes = new Map<string, NodeSummary>(Object.entries(published ?? {}));
  for (const [ref, summary] of own ?? []) nodes.set(ref, summary);
  return nodes;
}

export function totalDetections(nodes: Iterable<NodeSummary>): number {
  let total = 0;
  for (const summary of nodes) total += detectionCount(summary);
  return total;
}

/** A part of a whole as a percentage, or null while there is no whole to take
 *  it of: a network with no detections gives every node a dash, not 0%. */
export function shareOf(part: number, whole: number | null): number | null {
  return whole ? (part / whole) * 100 : null;
}

/** A share to one place. One too small for that place reads as under it, so a
 *  node that detected something never reads as having added nothing. */
export function formatShare(share: number | null): string {
  if (share === null || !Number.isFinite(share)) return DASH;
  if (share > 0 && share < 0.05) return "<0.1%";
  return formatPercent(share, 1);
}

/** A count and its noun: "1 node", "3 nodes". */
export function count(n: number, noun: string): string {
  return `${n.toLocaleString()} ${n === 1 ? noun : `${noun}s`}`;
}

/** A trust score, 0 to 1, as the leaderboard prints it: a whole percentage. */
export function formatTrust(score: number | null | undefined): string {
  return typeof score === "number" && Number.isFinite(score) ? `${(score * 100).toFixed(0)}%` : DASH;
}

/** What a node calls itself, as the leaderboard names it. The routes fall back
 *  to the whole ref for a node with no name of its own, and a ref reads as its
 *  short form everywhere else in the console. */
export function displayName(node: { name?: string | null; node_ref: string | null }): string {
  return node.name && node.name !== node.node_ref ? node.name : shortRef(node.node_ref) || "Unnamed node";
}

/** Names for every node the page lists, keyed by node_ref: the leaderboard's
 *  public names, with the owner's own names over their nodes. */
export function nodeNames(rows: readonly LeaderboardRow[], owned: readonly OwnedNode[]): Map<string, string> {
  const names = new Map<string, string>();
  for (const row of rows) names.set(row.node_ref, displayName(row));
  for (const node of owned) if (node.node_ref) names.set(node.node_ref, displayName(node));
  return names;
}

/** The leaderboard as the page reads it: each ranked node's row by node_ref,
 *  and how many nodes the ranks are out of. */
export interface Standings {
  rows: readonly LeaderboardRow[];
  byRef: ReadonlyMap<string, LeaderboardRow>;
  of: number;
}

export function readStandings(body: { leaderboard?: unknown; total?: unknown } | null | undefined): Standings {
  const rows = Array.isArray(body?.leaderboard) ? (body.leaderboard as LeaderboardRow[]) : [];
  const of = typeof body?.total === "number" ? body.total : rows.length;
  return { rows, byRef: new Map(rows.map((row) => [row.node_ref, row])), of };
}

/** Why an owned node has no rank, in the leaderboard page's words. The
 *  leaderboard ranks only what /api/radar/analytics publishes, which never
 *  includes a node registered private. Probation is a fact rather than the
 *  cause: whether it holds a radar back depends on a server switch the owner's
 *  list does not carry. */
export function unrankedReason(node: OwnedNode): string {
  if (node.location_private) return "Its location is private, so it is not published";
  if (node.polled?.trust_state === "probation") return "The radar is still on probation";
  return "No published figures yet";
}

/** One pair of nodes whose beams share coverage, as the association service
 *  publishes it. `grid_points` counts the points of its 3 km grid, at six
 *  altitudes, that lie in both beams: the size of the shared volume. */
export interface OverlapPair {
  node_a: string;
  node_b: string;
  grid_points: number;
  delay_gate_us: number | null;
  doppler_gate_hz: number | null;
}

export interface Overlaps {
  /** Largest shared volume first, so the order holds from one poll to the next. */
  pairs: OverlapPair[];
  /** The nodes the service holds coverage geometry for, or null when the
   *  answer did not say. Only these can overlap anything. */
  registered: ReadonlySet<string> | null;
}

const asNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/** /api/radar/association/overlaps, read defensively: a pair missing either
 *  node is dropped, and one flagged as not overlapping is not an overlap. */
export function readOverlaps(body: unknown): Overlaps {
  const answer = body as { overlaps?: unknown; registered_nodes?: unknown } | null | undefined;
  const raw: unknown[] = Array.isArray(body) ? body : Array.isArray(answer?.overlaps) ? answer.overlaps : [];
  const pairs = raw
    .filter(
      (p): p is Record<string, unknown> =>
        typeof p === "object" &&
        p !== null &&
        typeof (p as { node_a?: unknown }).node_a === "string" &&
        typeof (p as { node_b?: unknown }).node_b === "string" &&
        (p as { has_overlap?: unknown }).has_overlap !== false,
    )
    .map((p) => ({
      node_a: p.node_a as string,
      node_b: p.node_b as string,
      grid_points: asNumber(p.grid_points) ?? 0,
      delay_gate_us: asNumber(p.delay_gate_us),
      doppler_gate_hz: asNumber(p.doppler_gate_hz),
    }))
    .sort(
      (a, b) =>
        b.grid_points - a.grid_points || a.node_a.localeCompare(b.node_a) || a.node_b.localeCompare(b.node_b),
    );
  const listed = !Array.isArray(body) && Array.isArray(answer?.registered_nodes) ? answer.registered_nodes : null;
  const registered = listed ? new Set(listed.filter((r): r is string => typeof r === "string")) : null;
  return { pairs, registered };
}

/** A pair seen from one of the owner's nodes. A pair of two of theirs is
 *  listed once, from its first node, with the other marked as theirs too. */
export interface OwnOverlap {
  mine: string;
  other: string;
  pair: OverlapPair;
}

export function overlapsInvolving(pairs: readonly OverlapPair[], refs: ReadonlySet<string>): OwnOverlap[] {
  return pairs.flatMap((pair) => {
    if (refs.has(pair.node_a)) return [{ mine: pair.node_a, other: pair.node_b, pair }];
    if (refs.has(pair.node_b)) return [{ mine: pair.node_b, other: pair.node_a, pair }];
    return [];
  });
}

/** How many overlaps one node is part of, or why there is no count to give. */
export type OverlapReading =
  | { kind: "count"; count: number }
  /** The listing leaves private nodes out, so it says nothing either way. */
  | { kind: "private" }
  /** The service holds no coverage geometry for it, so nothing was computed. */
  | { kind: "not-computed" }
  | { kind: "unknown" };

export function overlapReading(node: OwnedNode, overlaps: Overlaps | null): OverlapReading {
  if (node.location_private) return { kind: "private" };
  if (!overlaps || !node.node_ref) return { kind: "unknown" };
  const ref = node.node_ref;
  if (overlaps.registered && !overlaps.registered.has(ref)) return { kind: "not-computed" };
  return { kind: "count", count: overlaps.pairs.filter((p) => p.node_a === ref || p.node_b === ref).length };
}
