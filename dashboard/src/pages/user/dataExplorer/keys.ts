/**
 * Archive keys and what can be read off one. Keys are Hive-partitioned
 * (`year=/month=/day=/node_id=/part-*.parquet`) with older ones carrying bare
 * directory names, so every segment is read through `value()`.
 */

/** The listing exposes only `modified`, written at the end of the hour the
 *  file covers (`ARCHIVE_FLUSH_INTERVAL_S`), so a span is an estimate. */
export const FILE_SPAN_MS = 3_600_000;

/** The download route serves the archived Parquet rebuilt as legacy per-frame
 *  JSON, which runs about nine times the stored size. */
export const JSON_FACTOR = 9;

export interface ArchiveFile {
  key: string;
  /** The last segment, for display. */
  name: string;
  node: string;
  /** UTC day, `YYYY-MM-DD`. */
  day: string;
  /** Stored bytes, Parquet not JSON. */
  size: number;
  startMs: number;
  endMs: number;
}

/** A partition segment's value: `node_id=ret-abc` and `ret-abc` both give
 *  `ret-abc`. Everything after the first `=`, which is the derivation the
 *  archive route itself uses, so the page and the route never disagree about
 *  which node a key belongs to. */
const value = (segment: string): string => {
  const eq = segment.indexOf("=");
  return eq === -1 ? segment : segment.slice(eq + 1);
};

export function parseKey(key: string, sizeBytes: unknown, modified: string): ArchiveFile | null {
  const parts = String(key).split("/").filter(Boolean);

  const endMs = Date.parse(modified);
  if (!Number.isFinite(endMs)) return null;

  // Four segments is the shallowest key that carries a date: three for the
  // day and one for the node, before the filename.
  if (parts.length < 4) return null;
  const day = `${value(parts[0])}-${value(parts[1])}-${value(parts[2])}`;

  return {
    key: String(key),
    name: parts[parts.length - 1],
    node: value(parts[parts.length - 2]),
    day,
    size: Number(sizeBytes) || 0,
    endMs,
    startMs: endMs - FILE_SPAN_MS,
  };
}
