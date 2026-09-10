// Detection-presence oracle update — pure so it can be unit-tested without
// mounting the aircraft hook.
//
// The oracle maps "hex|node_ref" → ts of the last time that node contributed a
// detection for that aircraft.  Two sources are unioned:
//
//  - per-aircraft signals from the feed entries (node_ref for single-node
//    tracks, contributing_node_refs for multinode solves), keyed on
//    ground_truth_hex when present so a multinode track (whose own hex is
//    synthetic) still joins to its aircraft;
//  - the top-level detecting_nodes feed key (hex → [node_ref]), which carries
//    the full per-node fan-out that the one-entry-per-hex aircraft list
//    cannot express (testmap debug feed only; absent on filtered feeds).
//
// Entries older than maxAgeMs are pruned in place — the same grace window as
// the arc buffer, so a detection that only just expired still counts.

export type DetectionMap = Record<string, number>;

interface DetectionSource {
  hex?: string;
  ground_truth_hex?: string;
  node_ref?: string;
  contributing_node_refs?: string[];
}

export function updateDetections(
  det: DetectionMap,
  newAircraft: DetectionSource[],
  detectingNodes: Record<string, string[]> | undefined | null,
  now: number,
  maxAgeMs: number,
): void {
  for (const ac of newAircraft || []) {
    const hex = ac.ground_truth_hex || ac.hex;
    if (!hex) continue;
    if (ac.node_ref) det[`${hex}|${ac.node_ref}`] = now;
    if (Array.isArray(ac.contributing_node_refs)) {
      for (const ref of ac.contributing_node_refs) det[`${hex}|${ref}`] = now;
    }
  }
  if (detectingNodes && typeof detectingNodes === "object") {
    for (const [hex, refs] of Object.entries(detectingNodes)) {
      if (!Array.isArray(refs)) continue;
      for (const ref of refs) det[`${hex}|${ref}`] = now;
    }
  }
  for (const key of Object.keys(det)) {
    if (now - det[key] > maxAgeMs) delete det[key];
  }
}

/** Node refs with a live (within maxAgeMs) detection entry for `hex`. */
export function detectingNodeRefsFor(
  det: DetectionMap,
  hex: string,
  now: number,
  maxAgeMs: number,
): string[] {
  const prefix = `${hex}|`;
  const out: string[] = [];
  for (const [key, ts] of Object.entries(det)) {
    if (!key.startsWith(prefix)) continue;
    if (now - ts > maxAgeMs) continue;
    out.push(key.slice(prefix.length));
  }
  return out.sort();
}
