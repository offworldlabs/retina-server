/**
 * Listing the archive a day at a time. The endpoint is date-bounded by
 * necessity: the no-date path caps out and is not newest-first, so it cannot
 * answer "what is there lately?".
 *
 * Two archives answer the same question. The public one lists every public
 * node; the owner's lists only the signed-in owner's nodes, private ones
 * included, and wants the session. Same parameters, same shape, same paging.
 * The owner's is asked through the console's client, as every owner route
 * is, so a 401 from it ends the session on the page at once.
 */

import { request } from "@retina/shared";

import { api, ownerArchivePath } from "../../../api/client";
import { type ArchiveFile, parseKey, type RowFacts } from "./keys";

/** The route's hard cap on a page. */
export const PAGE = 500;
/** A runaway guard on the paging loop, for a `total` that never arrives. */
export const MAX_OFFSET = 50_000;
/** A whole-day listing runs to seconds, well past the shared client's
 *  ten-second default, and the loop pays it once per page. The owner's route
 *  pages the same store the same way, and nginx gives it the same budget. */
export const LISTING_TIMEOUT_MS = 30_000;

/** Which archive a listing reads. */
export type ArchiveSource = "public" | "owner";

const PUBLIC_ARCHIVE = "/api/data/archive";

/** What a request to a public route sends as its Referer: the origin alone.
 *  The console's policy sends the page's whole address with a request to its
 *  own origin, and the explorer's address can name a node no public route may
 *  be told of: a private one a link brought in before any owner's list could
 *  say it was private. */
export const PUBLIC_REFERRER: ReferrerPolicy = "strict-origin";

/** Where one file is served, relative to the page's origin. The key goes on
 *  as it came: the server issued it, and the route takes it as a path. */
export function filePath(key: string, source: ArchiveSource): string {
  return source === "owner" ? ownerArchivePath(key) : `${PUBLIC_ARCHIVE}/${key}`;
}

/** Every key each archive has listed, in any day or scope read so far. */
export type Listed = Record<ArchiveSource, ReadonlySet<string>>;

/**
 * Which route serves a file to this viewer. Only an archive that listed the
 * key can be trusted to serve it, so `prefer` is taken when it did. Otherwise
 * the public route, which needs no session, then the owner's, which needs
 * one. A key neither has listed goes to `prefer`, the archive being read.
 */
export function routeFor(key: string, listed: Listed, prefer: ArchiveSource): ArchiveSource {
  if (listed[prefer].has(key)) return prefer;
  if (listed.public.has(key)) return "public";
  if (listed.owner.has(key)) return "owner";
  return prefer;
}

/** The keys a cache of listings holds. */
export function listedKeys(cache: Map<string, DayEntry>): ReadonlySet<string> {
  const keys = new Set<string>();
  for (const entry of cache.values()) for (const f of entry.files) keys.add(f.key);
  return keys;
}

export type DayStatus = "loading" | "done" | "error";

export interface DayEntry {
  day: string;
  /** The node this listing was scoped to, or null for the whole day. */
  nodeRef: string | null;
  status: DayStatus;
  files: ArchiveFile[];
  error?: string;
}

export function cacheKey(day: string, nodeRef: string | null): string {
  return `${day}|${nodeRef || "*"}`;
}

export async function fetchDayListing(
  day: string,
  nodeRef: string | null,
  signal?: AbortSignal,
  source: ArchiveSource = "public",
): Promise<ArchiveFile[]> {
  const files: ArchiveFile[] = [];
  const date = day.replace(/-/g, "/");
  const opts = { signal, timeoutMs: LISTING_TIMEOUT_MS };
  for (let offset = 0; ; offset += PAGE) {
    let page: { files?: unknown[]; total?: number };
    if (source === "owner") {
      page = await api.myArchive({ date, nodeRef, limit: PAGE, offset }, opts);
    } else {
      const params = new URLSearchParams({ date, limit: String(PAGE), offset: String(offset) });
      if (nodeRef) params.set("node_ref", nodeRef);
      page = await request(`${PUBLIC_ARCHIVE}?${params}`, { ...opts, referrerPolicy: PUBLIC_REFERRER });
    }

    const rows = page.files || [];
    for (const row of rows as ({ key: string; size_bytes: unknown; modified: string } & RowFacts)[]) {
      const file = parseKey(row.key, row.size_bytes, row.modified, row);
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
