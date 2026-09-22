/**
 * The dashboard's number and time formatters. Every page reads the same
 * payloads, so a metric must read the same on each of them; these are the one
 * copy. A value the payload did not carry renders as a dash rather than as
 * "NaN", "0h 0m" or "undefined".
 */

/** What a missing value renders as, everywhere. */
export const DASH = "—";

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

/** How long ago a moment was, in the coarsest unit that is not zero. Takes an
 *  ISO timestamp or epoch seconds, the two forms the backend sends; one that
 *  does not parse is as missing as an absent one. */
export function formatRelativeTime(at: string | number | null | undefined): string {
  if (!at) return DASH;
  const ms = typeof at === "number" ? at * 1000 : Date.parse(at);
  if (Number.isNaN(ms)) return DASH;
  const s = Math.round((Date.now() - ms) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** A span of seconds: whole seconds under a minute, whole minutes under an
 *  hour, tenths of an hour past that. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === undefined || seconds === null || Number.isNaN(seconds)) return DASH;
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

/** A percentage to fixed decimals, or a dash for a missing one. */
export function formatPercent(n: number | null | undefined, decimals = 1): string {
  const value = fmt(n, decimals);
  return value === DASH ? DASH : `${value}%`;
}

/** A byte count in B, KB, MB or GB. A day of archive across the fleet passes
 *  a gigabyte, so the largest step earns its place. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes)) return DASH;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
