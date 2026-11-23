/**
 * Whether a listed file can open on the archive map, and where. The map
 * replays the real fleet's public archive, so a file opens there at its first
 * frame and at its node's published position, read off the file's own listing
 * row. No registry is read for a position: the owner's holds the true ones,
 * which no link may carry.
 */

import type { Scope } from "../../../hooks/useScopeParam";
import { mapLink } from "../../map/mapLink";
import type { ArchiveFile } from "./keys";

/** Close enough to show a node's coverage. */
const ZOOM = 9;

/** No archive holds a frame before this, and the map drops an earlier `t`. */
const EARLIEST_FRAME_MS = Date.UTC(2020, 0, 1);

/** A link to the map, or the one line that says why there is none. */
export type MapTarget = { href: string } | { reason: string };

export interface MapScope {
  /** Whose archive is on screen. */
  scope: Scope;
  /** The owner's nodes that keep off the public archive, by node_ref. */
  privateRefs: ReadonlySet<string>;
  /** Whether a node is synthetic, by the scope's own registry. */
  synthetic: (id: string) => boolean;
}

const finite = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v);

/** Where "Show on map" takes a file, or why it cannot. The first rule that
 *  matches decides. */
export function showOnMap(
  file: Pick<ArchiveFile, "node" | "firstFrameMs" | "publishedLat" | "publishedLon">,
  { scope, privateRefs, synthetic }: MapScope,
): MapTarget {
  if (synthetic(file.node)) return { reason: "Synthetic node: the archive map shows the real fleet only." };
  if (scope === "mine" && privateRefs.has(file.node)) return { reason: "Private node: the public map leaves it out." };
  const t = file.firstFrameMs;
  // 0 is a frame stored without a time, and the map takes a `t` before 2020
  // for a typo and opens live instead, so neither says when the file is.
  if (!finite(t) || t < EARLIEST_FRAME_MS) return { reason: "This file's frame times are unknown." };
  const lat = file.publishedLat;
  const lon = file.publishedLon;
  if (!finite(lat) || !finite(lon)) return { reason: "This node has no published position." };
  return { href: mapLink({ lat, lon, z: ZOOM, t }) };
}
