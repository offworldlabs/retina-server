/**
 * The dashboard's number and time formatters. Every page reads the same
 * payloads, so a metric must read the same on each of them; these are the one
 * copy. A value the payload did not carry renders as a dash rather than as
 * "NaN", "0h 0m" or "undefined".
 */

const DASH = "—";

/** A number to fixed decimals, or a dash for a missing one. */
export function fmt(n: number | null | undefined, decimals = 2): string {
  if (n === undefined || n === null || Number.isNaN(n)) return DASH;
  return Number(n).toFixed(decimals);
}

/** Seconds of uptime as hours and minutes, or days and hours once past a day. */
export function formatUptime(seconds: number | null | undefined): string {
  if (!seconds) return DASH;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return `${h}h ${m}m`;
}

/** How long ago an ISO timestamp was, in the coarsest unit that is not zero. */
export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const diffS = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (diffS < 5) return "just now";
  if (diffS < 60) return `${diffS}s ago`;
  if (diffS < 3600) return `${Math.floor(diffS / 60)}m ago`;
  return `${Math.floor(diffS / 3600)}h ago`;
}

/** A byte count in B, KB or MB. Nothing here is big enough to need GB. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
