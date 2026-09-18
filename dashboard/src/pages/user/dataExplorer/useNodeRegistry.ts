/**
 * The fleet, as the radar describes it. Names, status and published positions
 * for the picker and the map; the archive listing itself names only ids.
 *
 * A failure is held rather than smoothed over: an empty registry and an
 * unreachable one look identical in the UI otherwise, and the second is the
 * one worth offering a retry for.
 */

import { useMemo } from "react";

import { request } from "@retina/shared";

import { useFetch } from "../../../hooks/usePolling";
import { isSyntheticNode } from "../../../utils/nodeKind";
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

export function useNodeRegistry(): NodeRegistry {
  const { data, loading, error, refresh } = useFetch<{ nodes?: Record<string, RawNode> }>(() =>
    request("/api/radar/nodes"),
  );

  const nodes = useMemo(() => {
    const raw = data?.nodes || {};
    const out = new Map<string, RegistryNode>();
    for (const id of Object.keys(raw)) out.set(id, toRegistryNode(id, raw[id] || {}));
    return out;
  }, [data]);

  return { nodes, loading, error: error ? error.message : null, retry: refresh };
}
