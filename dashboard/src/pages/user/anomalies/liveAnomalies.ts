import type { AircraftFrame, FeedAircraft, OwnedNode } from "../../../types";

/** One anomalous aircraft the owner's nodes see now. */
export interface LiveAnomaly {
  aircraft: FeedAircraft;
  /** Its anomaly types, "unknown" alone for a flag that names none. */
  types: string[];
  /** The owner's nodes behind it, in the order of the owner's list. */
  nodes: OwnedNode[];
  /** Nodes behind it that are not on the owner's list: other owners'. */
  others: number;
}

export interface LiveAnomalies {
  /** Ordered by hex, so a refresh does not reshuffle the table. */
  rows: LiveAnomaly[];
  /** Aircraft per type, most first. An aircraft flagged for two types counts
   *  under each. */
  byType: { type: string; count: number }[];
  /** How many of the owner's nodes see at least one of them. */
  nodesSeeing: number;
}

/** The node_refs behind an aircraft: the one node of a single-node track, the
 *  contributors of a solve or of a claim several nodes share. */
function refsOf(aircraft: FeedAircraft): string[] {
  const contributors = Array.isArray(aircraft.contributing_node_refs) ? aircraft.contributing_node_refs : [];
  const refs = [aircraft.node_ref, ...contributors];
  return [...new Set(refs.filter((ref): ref is string => typeof ref === "string" && ref !== ""))];
}

/**
 * The anomalous aircraft in the owner's frame, each with the owner's nodes
 * that see it. The route already keeps the frame to the caller's nodes; an
 * aircraft none of the listed nodes is behind is left out as well, so every
 * row names the node that saw it.
 */
export function liveAnomalies(frame: AircraftFrame | null, nodes: OwnedNode[]): LiveAnomalies {
  const byRef = new Map<string, OwnedNode>();
  for (const node of nodes) if (node.node_ref) byRef.set(node.node_ref, node);

  const rows: LiveAnomaly[] = [];
  const listed = frame?.aircraft;
  for (const aircraft of Array.isArray(listed) ? listed : []) {
    if (aircraft.is_anomalous !== true) continue;
    const refs = refsOf(aircraft);
    const mine = new Set(refs.filter((ref) => byRef.has(ref)));
    if (mine.size === 0) continue;
    const flagged = Array.isArray(aircraft.anomaly_types) ? aircraft.anomaly_types : [];
    const named = [...new Set(flagged.filter((t) => typeof t === "string" && t !== ""))];
    rows.push({
      aircraft,
      types: named.length > 0 ? named : ["unknown"],
      nodes: nodes.filter((node) => node.node_ref !== null && mine.has(node.node_ref)),
      others: refs.length - mine.size,
    });
  }
  rows.sort((a, b) => (a.aircraft.hex < b.aircraft.hex ? -1 : a.aircraft.hex > b.aircraft.hex ? 1 : 0));

  const counts = new Map<string, number>();
  for (const row of rows) for (const type of row.types) counts.set(type, (counts.get(type) ?? 0) + 1);
  const byType = [...counts]
    .map(([type, count]) => ({ type, count }))
    .sort((a, b) => b.count - a.count || (a.type < b.type ? -1 : a.type > b.type ? 1 : 0));

  const seeing = new Set(rows.flatMap((row) => row.nodes.map((node) => node.node_id)));
  return { rows, byType, nodesSeeing: seeing.size };
}
