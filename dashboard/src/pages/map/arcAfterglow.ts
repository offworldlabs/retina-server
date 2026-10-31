// How one detection arc in the afterglow buffer appears on the map: whether
// it is drawn, at what opacity, and along which locus.  Pure, so the decision
// is testable without Leaflet; DetectionArcs applies it to its polylines.
//
// Age is read on the map's clock: the wall clock live, the replayed instant
// in archive mode, where every buffer entry is stamped with a detection's own
// time days in the past.

import type { ArcBufferEntry } from "./arcBuffer";
import { buildBistaticArc, type NodeGeometry } from "./bistaticArc";
import { ARC_FADE_MS, ARC_HOLD_MS, ARC_TOTAL_LIFE_MS } from "./constants";

/**
 * The opacity of an arc when the map's clock reads `nowMs`, or null when it
 * is not drawn: it has no usable locus, it is past its life, or it is stamped
 * after the clock (a replay sought back before its stores were cleared).
 * ARC_HOLD_MS at full opacity, then a linear fade over ARC_FADE_MS.
 */
export function arcOpacity(
  entry: Pick<ArcBufferEntry, "ambiguity_arc" | "ts">,
  nowMs: number,
): number | null {
  if (!Array.isArray(entry.ambiguity_arc) || entry.ambiguity_arc.length < 2) return null;
  const age = nowMs - entry.ts;
  if (age < 0 || age > ARC_TOTAL_LIFE_MS) return null;
  if (age <= ARC_HOLD_MS) return 1.0;
  return Math.max(0.0, 1.0 - (age - ARC_HOLD_MS) / ARC_FADE_MS);
}

/**
 * The locus a new arc's polyline is drawn along.
 *
 * Archived: the entry's own ambiguity_arc, exactly as given.  The replay
 * built it from the receiver and transmitter the archive holds for that
 * detection, and the node registry holds where the receiver is today, not
 * where it was.
 *
 * Live: rebuilt from the MEASURED delay_us stored in the buffer entry —
 * never from the dead-reckoned icon position.  (The old icon-delay rebuild
 * replaced a fixed measurement with a moving icon-implied delay: staging
 * measured 180/190 consecutive ticks where delay_us was unchanged while the
 * icon-implied delay drifted p90 0.51 km/tick, painting five offset parallel
 * strokes for one measurement — and collapsed a ~30 km arc to a ~1 µs blob
 * when the icon sat near the TX–RX baseline.)  The rebuilt locus spans the
 * node's whole detection area (same semantics as the backend arc) and is
 * altitude-agnostic (2026-08 direction) — entry.alt_baro is display data
 * only here, it does not shape the geometry.  Falls back to the backend arc
 * when node geometry (useNodes still loading) or delay_us is missing.
 */
export function arcGeometry(
  entry: Pick<ArcBufferEntry, "ambiguity_arc" | "delay_us" | "node_ref">,
  archived: boolean,
  nodesByRef: Record<string, NodeGeometry> | null | undefined,
): [number, number][] {
  if (archived) return entry.ambiguity_arc;
  if (entry.node_ref && entry.delay_us != null && entry.delay_us > 0) {
    const node = nodesByRef?.[entry.node_ref];
    if (node) {
      const rebuilt = buildBistaticArc(entry.delay_us, node);
      if (rebuilt && rebuilt.length >= 2) return rebuilt;
    }
  }
  return entry.ambiguity_arc;
}
