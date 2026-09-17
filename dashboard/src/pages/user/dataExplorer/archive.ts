/**
 * Listing the archive a day at a time. The endpoint is date-bounded by
 * necessity: the no-date path caps out and is not newest-first, so it cannot
 * answer "what is there lately?".
 */

import { request } from "@retina/shared";

import { type ArchiveFile, parseKey } from "./keys";

/** The route's hard cap on a page. */
export const PAGE = 500;
/** A runaway guard on the paging loop, for a `total` that never arrives. */
export const MAX_OFFSET = 50_000;
/** A whole-day listing runs to seconds, well past the shared client's
 *  ten-second default, and the loop pays it once per page. */
export const LISTING_TIMEOUT_MS = 30_000;

export type DayStatus = "loading" | "done" | "error";

export interface DayEntry {
  day: string;
  /** The node this listing was scoped to, or null for the whole day. */
  nodeId: string | null;
  status: DayStatus;
  files: ArchiveFile[];
  error?: string;
}

export function cacheKey(day: string, nodeId: string | null): string {
  return `${day}|${nodeId || "*"}`;
}

export async function fetchDayListing(
  day: string,
  nodeId: string | null,
  signal?: AbortSignal,
): Promise<ArchiveFile[]> {
  const files: ArchiveFile[] = [];
  for (let offset = 0; ; offset += PAGE) {
    const params = new URLSearchParams({
      date: day.replace(/-/g, "/"),
      limit: String(PAGE),
      offset: String(offset),
    });
    if (nodeId) params.set("node_id", nodeId);

    const page = await request<{ files?: unknown[]; total?: number }>(
      `/api/data/archive?${params}`,
      { signal, timeoutMs: LISTING_TIMEOUT_MS },
    );

    const rows = page.files || [];
    for (const row of rows as { key: string; size_bytes: unknown; modified: string }[]) {
      const file = parseKey(row.key, row.size_bytes, row.modified);
      if (file) files.push(file);
    }

    // `total` decides, not a short page: the route drops private nodes after
    // paging, so a page shortens without the list ending.
    const total = Number(page.total);
    const done = Number.isFinite(total) && total >= 0 ? offset + PAGE >= total : rows.length < PAGE;
    if (done || offset + PAGE >= MAX_OFFSET) return files;
  }
}

/** The cached listing that can answer for this selection, if any. A whole-day
 *  listing serves any subset; a node-scoped one serves only the case where
 *  that node is the entire selection. So narrowing reuses what is loaded and
 *  widening refetches. */
export function entryForScope(
  cache: Map<string, DayEntry>,
  day: string,
  effective: Set<string>,
): DayEntry | null {
  const whole = cache.get(cacheKey(day, null));
  if (whole) return whole;
  if (effective.size !== 1) return null;
  const [only] = effective;
  return cache.get(cacheKey(day, only)) || null;
}

/** The oldest day still served from local disk, read off the data rather than
 *  from configuration. Withheld unless every day before it has finished and
 *  come back empty: naming one mid-scan invites the next response to
 *  contradict it. */
export function horizon(
  cache: Map<string, DayEntry>,
  days: string[],
  effective: Set<string>,
): string | null {
  const settled = (day: string) => {
    const e = entryForScope(cache, day, effective);
    return e && e.status === "done" ? e : null;
  };

  const oldestWithFiles = days.find((day) => {
    const e = settled(day);
    return e && e.files.length > 0;
  });
  if (!oldestWithFiles) return null;

  let emptiesBefore = 0;
  for (const day of days) {
    if (day >= oldestWithFiles) break;
    const e = settled(day);
    if (!e) return null;
    if (e.files.length > 0) return null;
    emptiesBefore += 1;
  }
  return emptiesBefore > 0 ? oldestWithFiles : null;
}
