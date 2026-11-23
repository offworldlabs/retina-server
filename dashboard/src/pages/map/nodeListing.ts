/**
 * How an analytics entry becomes a node on the map, and how the signed-in
 * owner's own nodes take the place of their public entries.
 *
 * Two routes answer in one shape, keyed on node_ref. GET /api/radar/analytics
 * is the public listing: the same bytes for every caller, so a private node is
 * left out for its owner as well, and every receiver in it is displaced by the
 * backend (services/public_location.py). GET /api/auth/me/analytics is the
 * owner's: their own nodes, private ones included, with nothing moved. Both
 * entries go through radarNodeFrom, so a node is drawn the same way whichever
 * route it came from; what differs is where its receiver is and the
 * uncertainty the entry declares about it.
 */

import type { OwnedNode, OwnerAnalytics } from "../../types";
import { isSyntheticNode } from "../../utils/nodeKind";
import type { FeedMode } from "./feedMode";
import type { RadarNode } from "./types";

/** One end of a detection area, as either analytics route serves it. */
interface AreaEnd {
  lat: number;
  lon: number;
  alt?: number | null;
  /** The receiver's, and only where the backend displaced it. */
  location_uncertainty_km?: number | null;
}

/** An entry of either analytics map, as far as the map reads one. */
export interface AnalyticsEntry {
  is_synthetic?: boolean;
  detection_area?: {
    rx: AreaEnd;
    tx: AreaEnd;
    beam_azimuth_deg: number;
    beam_width_deg: number;
    max_range_km: number;
    max_bistatic_range_km?: number | null;
  } | null;
  empirical_coverage?: {
    polygon?: [number, number][] | null;
    n_points?: number | null;
    polygon_source?: RadarNode["empirical_polygon_source"];
  } | null;
}

/** Whether a node belongs on a page showing `mode`'s fleet (feedMode.ts). */
export function inFleet(mode: FeedMode, synthetic: boolean): boolean {
  if (mode === "real") return !synthetic;
  if (mode === "synthetic") return synthetic;
  return true;
}

/**
 * The map's node for one analytics entry, or null when the entry has no
 * detection area to place it by. `synthetic` is resolved by the caller, from
 * whichever record carries the server's flag: the public entry carries it, the
 * owner's summary does not and the owner's node record does.
 */
export function radarNodeFrom(ref: string, info: AnalyticsEntry, synthetic: boolean): RadarNode | null {
  const da = info.detection_area;
  const ec = info.empirical_coverage;
  if (!da) return null;
  // Defence in depth, not the primary guard: the analytics library
  // only builds a detection_area when has_full_geometry(config) is
  // true, and that already rejects the exact rx=(0,0) sentinel, so
  // a null-island node should never reach here with one. Kept in
  // case some other path ever hands us a detection_area without
  // going through that check. Use a small epsilon so we still allow
  // a real node legitimately near the equator/prime meridian, but
  // dismiss the exact-zero sentinel that would otherwise render as
  // a stray marker in the Gulf of Guinea.
  const rxLat = da.rx.lat;
  const rxLon = da.rx.lon;
  if (Math.abs(rxLat) < 1e-6 && Math.abs(rxLon) < 1e-6) return null;
  return {
    // The map's key for this node: both analytics maps are keyed on
    // the public ref and the values carry no id of their own, so the
    // ref is both the join key and the label helper's input.
    node_ref: ref,
    // Used as served. On the public listing it is privacy-fuzzed by
    // the backend, which builds its published arcs around this same
    // anchor, so a client-side rebuild lands on the backend's curve.
    // On the owner's own entry it is the true receiver, and the
    // rebuild lands on the true ellipse instead.
    rx_lat: rxLat,
    rx_lon: rxLon,
    // The backend's own declaration of how far it displaced the
    // receiver. Absent when fuzzing is off, and on the owner's own
    // entry, which nothing moved; either resolves to 0 and
    // suppresses the uncertainty disc rather than drawing one of
    // zero radius.
    location_uncertainty_km: da.rx.location_uncertainty_km ?? 0,
    tx_lat: da.tx.lat,
    tx_lon: da.tx.lon,
    // Node altitudes (m ASL) for the altitude-corrected arc
    // rebuild.  The analytics detection_area payload currently
    // emits rx/tx as {lat, lon} only (no alt field), so these
    // resolve to null and buildBistaticArc falls back to
    // h_rx = h_tx = 0.  Read defensively so the values are picked
    // up automatically if the backend starts emitting them.
    rx_alt_m: da.rx.alt ?? null,
    tx_alt_m: da.tx.alt ?? null,
    beam_azimuth_deg: da.beam_azimuth_deg,
    beam_width_deg: da.beam_width_deg,
    max_range_km: da.max_range_km,
    // Null for a node that declares no differential limit, which
    // keeps the legacy circular sector.
    max_bistatic_range_km: da.max_bistatic_range_km ?? null,
    empirical_polygon: ec?.polygon ?? null,
    empirical_n_points: ec?.n_points ?? 0,
    // Absent on a payload from a server older than the declared
    // wedge: evidence-only is what every node published then, and
    // is the conservative reading either way.
    empirical_polygon_source: ec?.polygon_source ?? "evidence",
    is_synthetic: synthetic,
  };
}

/**
 * The owner's nodes as the map draws them: every one with analytics, private
 * ones included, at its true position and carrying its publication choice.
 *
 * The summaries say where a node is and the node records say what it is, so a
 * node has to be in both answers. One in only one of them was claimed or
 * released between the two requests; it is left out here, and its public
 * entry, if it has one, stays on the map.
 */
export function ownedMapNodes(owned: OwnedNode[], analytics: OwnerAnalytics, mode: FeedMode): RadarNode[] {
  const byRef = new Map<string, OwnedNode>();
  for (const node of Array.isArray(owned) ? owned : []) {
    if (node.node_ref) byRef.set(node.node_ref, node);
  }
  const nodes: RadarNode[] = [];
  for (const [ref, summary] of Object.entries(analytics?.nodes ?? {})) {
    const node = byRef.get(ref);
    if (!node) continue;
    const synthetic = isSyntheticNode(node, ref);
    // The page's fleet holds here as it does on the public listing: an
    // owner's synthetic node stays off a real-fleet page.
    if (!inFleet(mode, synthetic)) continue;
    const drawn = radarNodeFrom(ref, summary as unknown as AnalyticsEntry, synthetic);
    if (drawn) nodes.push({ ...drawn, location_private: Boolean(node.location_private) });
  }
  return nodes;
}

/**
 * Whether the map holds this node from the owner API: the one kind of node it
 * holds at a position no public route serves. Only ownedMapNodes sets
 * location_private, and it sets it on every node it makes; no public entry
 * has the field.
 */
export function isOwnerEntry(node: RadarNode): boolean {
  return node.location_private !== undefined;
}

/**
 * The public listing with the owner's nodes in it: each owned node in place of
 * its public entry, so it is drawn once and where it really is, and the ones
 * the public listing leaves out (the private ones) after the rest.
 *
 * In place rather than appended, so the public nodes keep their order and the
 * markers drawn from them their stacking. With no owned nodes to add the
 * listing comes back as it was given, the same array, so a caller memoising on
 * it is not woken.
 */
export function withOwnedNodes(listed: RadarNode[], owned: RadarNode[] | null): RadarNode[] {
  if (!owned || owned.length === 0) return listed;
  const byRef = new Map(owned.map((n) => [n.node_ref, n]));
  const merged = listed.map((n) => byRef.get(n.node_ref) ?? n);
  const placed = new Set(listed.map((n) => n.node_ref));
  for (const n of owned) {
    if (!placed.has(n.node_ref)) merged.push(n);
  }
  return merged;
}
