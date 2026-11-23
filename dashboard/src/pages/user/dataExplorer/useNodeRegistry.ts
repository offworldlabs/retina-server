/**
 * The fleet, as the radar describes it. Names, status and published positions
 * for the picker and the map; the archive listing itself names only ids.
 * When the radar's list needs API access the viewer lacks, status and
 * positions come from the public map instead, which names no node, so each
 * goes by its id.
 *
 * A failure is held rather than smoothed over: an empty registry and an
 * unreachable one look identical in the UI otherwise, and the second is the
 * one worth offering a retry for.
 *
 * The owner's view reads its nodes from the owner's own list instead, which
 * has the true positions and the private nodes the radar leaves out.
 */

import { useMemo } from "react";

import { HttpError, request } from "@retina/shared";

import { useFetch } from "../../../hooks/usePolling";
import type { OwnedNode } from "../../../types";
import { isSyntheticNode } from "../../../utils/nodeKind";
import { PUBLIC_REFERRER } from "./archive";
import type { RegistryNode } from "./nodes";

interface RawLocation {
  rx_lat?: unknown;
  rx_lon?: unknown;
  location_uncertainty_km?: unknown;
}

interface RawNode {
  name?: string;
  status?: string;
  is_synthetic?: boolean;
  location?: RawLocation;
}

interface Fleet {
  nodes?: Record<string, RawNode>;
}

export interface NodeRegistry {
  nodes: Map<string, RegistryNode>;
  /** True until the first request settles. An empty fleet and one that has
   *  not arrived yet say different things and must not read the same. */
  loading: boolean;
  error: string | null;
  retry: () => void;
}

/** A real number or nothing. `Number(null)` is 0, so a null coordinate would
 *  otherwise place the node on the equator. */
const finite = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

function toRegistryNode(id: string, raw: RawNode): RegistryNode {
  const location = raw.location || {};
  const lat = finite(location.rx_lat);
  const lon = finite(location.rx_lon);
  // Half a position is none: a node with a latitude and no longitude cannot be
  // drawn or measured against a radius.
  const placed = lat !== null && lon !== null;
  return {
    id,
    name: raw.name || id,
    status: raw.status ?? null,
    synthetic: isSyntheticNode(raw, id),
    lat: placed ? lat : null,
    lon: placed ? lon : null,
    uncertaintyKm: finite(location.location_uncertainty_km),
  };
}

/** Asked of a registry that is not wanted: settles at once with nothing, so
 *  no request leaves the page. */
const idle = () => Promise.resolve(null);

/** Whether the radar turned the viewer away rather than failed to answer: 403
 *  for an account without API access, 401 for no session at all. A rate limit
 *  or a server error is a failure like any other. */
const refused = (reason: unknown): boolean =>
  reason instanceof HttpError && (reason.status === 401 || reason.status === 403);

/** The radar's list, or for a viewer it refuses, the public map's: the same
 *  real nodes at the same published positions, without names. Should the map
 *  fail as well, its failure is the one shown; a retry starts at the radar. */
async function fleet(): Promise<Fleet> {
  try {
    return await request<Fleet>("/api/radar/nodes", { referrerPolicy: PUBLIC_REFERRER });
  } catch (reason) {
    if (!refused(reason)) throw reason;
    return request<Fleet>("/api/map/nodes", { referrerPolicy: PUBLIC_REFERRER });
  }
}

/** `enabled` false asks for nothing until it turns true, for a page showing a
 *  registry of its own meanwhile. */
export function useNodeRegistry(enabled = true): NodeRegistry {
  const { data, loading, pending, error, refresh } = useFetch<Fleet | null>(
    enabled ? fleet : idle,
    enabled ? "radar" : "idle",
  );

  const nodes = useMemo(() => {
    const raw = data?.nodes || {};
    const out = new Map<string, RegistryNode>();
    for (const id of Object.keys(raw)) out.set(id, toRegistryNode(id, raw[id] || {}));
    return out;
  }, [data]);

  // Loading until the fleet's own first answer. The idle answer settles the
  // hook without being one, so a registry enabled later has to read as loading
  // rather than as an empty fleet; a retry after a failure keeps the failure
  // up, as it always has, rather than reading as a first load.
  const arriving = enabled && (loading || (pending && data === null && error === null));

  return { nodes, loading: arriving, error: enabled && error ? error.message : null, retry: refresh };
}

/**
 * The signed-in owner's nodes as a registry: the true receiver position
 * rather than the published one, private nodes included, and no uncertainty,
 * since nothing is displaced. Keyed by node_ref, which is what an archive key
 * names; a node with no ref has no file that could name it and is left out.
 */
export function ownerRegistry(nodes: readonly OwnedNode[]): Map<string, RegistryNode> {
  const out = new Map<string, RegistryNode>();
  for (const node of nodes) {
    const ref = node.node_ref;
    if (!ref) continue;
    const lat = finite(node.location?.rx_lat ?? node.rx_lat);
    const lon = finite(node.location?.rx_lon ?? node.rx_lon);
    const placed = lat !== null && lon !== null;
    out.set(ref, {
      id: ref,
      name: node.name || ref,
      status: node.status ?? null,
      synthetic: isSyntheticNode({ is_synthetic: node.is_synthetic }, ref),
      lat: placed ? lat : null,
      lon: placed ? lon : null,
      uncertaintyKm: null,
    });
  }
  return out;
}
