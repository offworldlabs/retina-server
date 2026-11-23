/**
 * The explorer's filters and their query string. Every filter lives in the URL
 * so any view is a link, and the parameter shape is fixed: links to it are
 * already in circulation.
 */

import type { Scope } from "../../../hooks/useScopeParam";
import { addDays } from "./dates";

export const TOD_START = "00:00";
export const TOD_END = "23:59";
/** Below this a radius selects nothing useful, and a click on the map would
 *  land outside its own circle. */
export const MIN_RADIUS_KM = 2;
/** What a centre placed on the map starts with. Wide enough that the first
 *  click selects a neighbourhood rather than a rounding error. */
export const DEFAULT_RADIUS_KM = 50;

/** Carries "no nodes at all". Its own parameter rather than a reserved value
 *  of `node`, so it can never be confused with a node whose id says none. */
const NO_NODES = "nodes";
const NO_NODES_VALUE = "none";

/** Whose archive to list. Spelled as every other page spells it: the whole
 *  network by name, and the owner's own nodes, the default, by its absence,
 *  so a link from before there was a choice opens as it always did. */
const SCOPE = "scope";
const SCOPE_NETWORK = "network";

const DEFAULT_RANGE_DAYS = 3;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const TOD_RANGE = /^(\d{2}:\d{2})-(\d{2}:\d{2})$/;

/** A radius filter measured from a point. */
export interface NearPoint {
  lat: number;
  lon: number;
  km: number;
}

/** A radius filter measured from one of the signed-in owner's own nodes. The
 *  link names the node and never says where it is: that position is the
 *  owner's alone, and only their own list can place the node again. */
export interface NearNode {
  node: string;
  km: number;
}

export type NearFilter = NearPoint | NearNode;

export const isNearNode = (near: NearFilter | null): near is NearNode => near !== null && "node" in near;

/** What a node in a `near` centre may be spelled as: an id, which never reads
 *  as a number, so the two shapes of the parameter cannot be confused. */
const NEAR_NODE = /^[A-Za-z_][\w.-]*$/;

export interface ExplorerFilters {
  /** null means every node there is, including ones discovered later, rather
   *  than a snapshot of the ids known when it was set. */
  nodeSel: Set<string> | null;
  /** Inclusive UTC days. */
  from: string;
  to: string;
  todFrom: string;
  todTo: string;
  /** Stored bytes; 0 is off. */
  minSize: number;
  near: NearFilter | null;
  /** "mine" is the default and is never written. A viewer with no nodes of
   *  their own reads it as the whole network: it asks for the owner's view
   *  where there is one, and does not promise there is. */
  scope: Scope;
}

export function defaultFilters(today: string): ExplorerFilters {
  return {
    nodeSel: null,
    from: addDays(today, -(DEFAULT_RANGE_DAYS - 1)),
    to: today,
    todFrom: TOD_START,
    todTo: TOD_END,
    minSize: 0,
    near: null,
    scope: "mine",
  };
}

export function readFilters(search: string, today: string): ExplorerFilters {
  const q = new URLSearchParams(search);
  const f = defaultFilters(today);

  const nodes = q.getAll("node").filter(Boolean);
  if (nodes.length) f.nodeSel = new Set(nodes);
  else if (q.get(NO_NODES) === NO_NODES_VALUE) f.nodeSel = new Set();

  const from = q.get("from");
  const to = q.get("to");
  if (ISO_DAY.test(from)) f.from = from;
  if (ISO_DAY.test(to)) f.to = to;
  if (f.from > f.to) [f.from, f.to] = [f.to, f.from];

  const tod = TOD_RANGE.exec(q.get("tod") || "");
  if (tod) {
    f.todFrom = tod[1];
    f.todTo = tod[2];
  }

  const nearParts = String(q.get("near") || "").split(",");
  const near = nearParts.map(Number);
  if (near.length === 3 && near.every(Number.isFinite)) {
    f.near = { lat: near[0], lon: near[1], km: Math.max(MIN_RADIUS_KM, near[2]) };
  } else if (near.length === 2 && NEAR_NODE.test(nearParts[0]) && nearParts[1] !== "" && Number.isFinite(near[1])) {
    f.near = { node: nearParts[0], km: Math.max(MIN_RADIUS_KM, near[1]) };
  }

  const minSize = Number(q.get("minsize"));
  if (Number.isFinite(minSize) && minSize > 0) f.minSize = minSize;

  if (q.get(SCOPE) === SCOPE_NETWORK) f.scope = "network";

  return f;
}

export function writeFilters(f: ExplorerFilters): URLSearchParams {
  const qs = new URLSearchParams();
  // Sorted, so one selection has one link. An empty selection needs a
  // parameter of its own: absent `node` params already mean every node, so
  // without this "none" would come back from the URL as "all".
  if (f.nodeSel && f.nodeSel.size === 0) qs.set(NO_NODES, NO_NODES_VALUE);
  else if (f.nodeSel) Array.from(f.nodeSel).sort().forEach((n) => qs.append("node", n));
  qs.set("from", f.from);
  qs.set("to", f.to);
  if (f.todFrom !== TOD_START || f.todTo !== TOD_END) qs.set("tod", `${f.todFrom}-${f.todTo}`);
  // A point to four decimals, the same precision a click on the map quantises
  // to, so the link and the map agree about where the centre is. A node by
  // its id alone.
  if (isNearNode(f.near)) qs.set("near", `${f.near.node},${f.near.km}`);
  else if (f.near) qs.set("near", `${f.near.lat.toFixed(4)},${f.near.lon.toFixed(4)},${f.near.km}`);
  if (f.minSize) qs.set("minsize", String(f.minSize));
  // Last, so a link that names no scope keeps the shape it has always had.
  if (f.scope === "network") qs.set(SCOPE, SCOPE_NETWORK);
  return qs;
}
