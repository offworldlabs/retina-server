/**
 * What the map is allowed to say about a node, and where it may draw it.
 *
 * Two rules live here, both of them about honesty rather than presentation.
 *
 * **A node is named by its `node_ref`, never by its `node_id`.**  The id comes
 * off the board and is the name its owner gave the machine; the ref is the
 * public handle the backend now serves for every node, registered or not (see
 * backend/services/node_ref.py).  The id stays the join key everywhere in the
 * client — `nodesById`, `ac.node_id`, `contributing_node_ids`, selection state
 * — and only the displayed text changes.
 *
 * **One marker per SITE, not per node.**  Co-located receivers are published
 * at exactly equal coordinates on purpose: they share one fuzz offset so the
 * pair leaks one sample of its position rather than two (see
 * backend/services/node_sites.py).  A marker per node therefore stacked two
 * identical glyphs and two identical uncertainty discs at one point — the
 * lower node was unclickable and the doubled fill made the site look more
 * certain than a single node's, which is the opposite of the truth.
 */

import { haversineDistanceKm } from "./geo";
import type { RadarNode } from "../../types";

/** A receive site: one published coordinate, one or more nodes at it. */
export interface NodeSite {
  /** Grouping key — the published coordinate at the precision it is served. */
  key: string;
  rx_lat: number;
  rx_lon: number;
  /** The widest declaration at this site: the disc has to cover every member. */
  location_uncertainty_km: number;
  /** Members, ordered by label so the popup does not reshuffle between polls. */
  nodes: RadarNode[];
  /** True only when EVERY member is synthetic — one real node makes it real. */
  isSynth: boolean;
}

/** What a node is called on screen. */
export function nodeLabel(node: RadarNode | undefined | null): string {
  return node?.node_ref ?? "unlisted node";
}

/** Mirrors the backend's is_synthetic_node() prefix. */
function isSyntheticNode(node: RadarNode): boolean {
  return Boolean(node.node_id?.startsWith("synth-"));
}

/**
 * Group nodes into sites by exact published coordinate.
 *
 * Exact equality, not proximity, and for the same reason the backend groups
 * that way: co-located nodes are *configured* at one coordinate, so equality
 * is what a shared site looks like in the data, and a proximity rule would
 * make a node's marker depend on which of its neighbours happened to be
 * online.  The backend serves receiver coordinates at four decimals
 * (public_location.py) and the polygon apex at five; the key uses five, the
 * finer of the two, so two nodes published at one point compare equal here
 * and nothing the feed kept apart is ever merged.
 */
export function groupNodesBySite(nodes: RadarNode[]): NodeSite[] {
  const sites = new Map<string, NodeSite>();
  for (const n of nodes) {
    const key = `${n.rx_lat.toFixed(5)},${n.rx_lon.toFixed(5)}`;
    const existing = sites.get(key);
    if (existing) {
      existing.nodes.push(n);
      existing.location_uncertainty_km = Math.max(
        existing.location_uncertainty_km,
        n.location_uncertainty_km || 0,
      );
      existing.isSynth = existing.isSynth && isSyntheticNode(n);
    } else {
      sites.set(key, {
        key,
        rx_lat: n.rx_lat,
        rx_lon: n.rx_lon,
        location_uncertainty_km: n.location_uncertainty_km || 0,
        nodes: [n],
        isSynth: isSyntheticNode(n),
      });
    }
  }
  for (const site of sites.values()) {
    site.nodes.sort((a, b) => nodeLabel(a).localeCompare(nodeLabel(b)));
  }
  return [...sites.values()];
}

/**
 * How far the measured coverage reaches, in whole km: the distance to the
 * furthest polygon vertex from the receiver.
 *
 * The polygon is the answer to "where has this node been seen to detect", so
 * its outermost vertex is the only reach figure the map can quote without
 * inventing one — the node's declared `max_range_km` is configuration, and
 * quoting it beside a measured area would read as a measurement.  Returns null
 * when there is no polygon to measure.
 */
export function polygonMaxReachKm(
  rxLat: number,
  rxLon: number,
  polygon: [number, number][] | null | undefined,
): number | null {
  if (!Array.isArray(polygon) || polygon.length === 0) return null;
  let max = 0;
  for (const [lat, lon] of polygon) {
    const d = haversineDistanceKm(rxLat, rxLon, lat, lon);
    if (d > max) max = d;
  }
  return Math.round(max);
}
