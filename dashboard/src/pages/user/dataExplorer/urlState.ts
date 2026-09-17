/**
 * The explorer's filters and their query string. Every filter lives in the URL
 * so any view is a link, and the parameter shape is fixed: links to it are
 * already in circulation.
 */

import { addDays } from "./dates";

export const TOD_START = "00:00";
export const TOD_END = "23:59";
/** Below this a radius selects nothing useful, and a click on the map would
 *  land outside its own circle. */
export const MIN_RADIUS_KM = 2;

const DEFAULT_RANGE_DAYS = 3;
const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;
const TOD_RANGE = /^(\d{2}:\d{2})-(\d{2}:\d{2})$/;

export interface NearFilter {
  lat: number;
  lon: number;
  km: number;
}

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
  };
}

export function readFilters(search: string, today: string): ExplorerFilters {
  const q = new URLSearchParams(search);
  const f = defaultFilters(today);

  const nodes = q.getAll("node").filter(Boolean);
  if (nodes.length) f.nodeSel = new Set(nodes);

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

  const near = String(q.get("near") || "").split(",").map(Number);
  if (near.length === 3 && near.every(Number.isFinite)) {
    f.near = { lat: near[0], lon: near[1], km: Math.max(MIN_RADIUS_KM, near[2]) };
  }

  const minSize = Number(q.get("minsize"));
  if (Number.isFinite(minSize) && minSize > 0) f.minSize = minSize;

  return f;
}

export function writeFilters(f: ExplorerFilters): URLSearchParams {
  const qs = new URLSearchParams();
  // Sorted, so one selection has one link. Omitted when the selection is
  // everything, which is also how an empty one comes out, so an empty
  // selection is not round-trippable.
  if (f.nodeSel) Array.from(f.nodeSel).sort().forEach((n) => qs.append("node", n));
  qs.set("from", f.from);
  qs.set("to", f.to);
  if (f.todFrom !== TOD_START || f.todTo !== TOD_END) qs.set("tod", `${f.todFrom}-${f.todTo}`);
  // Four decimals, the same precision a click on the map quantises to, so the
  // link and the map agree about where the centre is.
  if (f.near) qs.set("near", `${f.near.lat.toFixed(4)},${f.near.lon.toFixed(4)},${f.near.km}`);
  if (f.minSize) qs.set("minsize", String(f.minSize));
  return qs;
}
