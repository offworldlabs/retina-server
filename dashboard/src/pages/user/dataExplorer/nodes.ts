/**
 * The node side of the filters: who exists, and which of them the radius
 * admits. Kept apart from `filters.ts`, which asks only whether a file's own
 * fields pass and takes the node set as given.
 */

import { isSyntheticNode } from "../../../utils/nodeKind";
import type { NearFilter } from "./urlState";

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

const EARTH_RADIUS_KM = 6371;

const toRadians = (degrees: number): number => (degrees * Math.PI) / 180;

/**
 * Great-circle distance. The haversine form rather than the cosine rule,
 * which loses its precision over the short separations these nodes sit at.
 *
 * A copy of the map's pages/map/geo.ts and distance.ts, whose pair carries
 * Leaflet types this page has no use for. It stands until the map's pair sheds
 * them or moves to packages/shared, which should then take this one too.
 */
export function distanceKm(aLat: number, aLon: number, bLat: number, bLon: number): number {
  const dLat = toRadians(bLat - aLat);
  const dLon = toRadians(bLon - aLon);
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRadians(aLat)) * Math.cos(toRadians(bLat)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_KM * Math.asin(Math.sqrt(h));
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
  near: NearFilter | null,
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
