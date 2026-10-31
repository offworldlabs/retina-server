import type { ReactNode } from "react";
import { DASH, formatDuration } from "../../../utils/format";

/** A key the server sent as it stands, in words: `returned_to_probation` reads
 *  "Returned to probation". For a kind or a reading the console has no wording
 *  for yet, which still arrives and still deserves a line. */
export function humanize(key: string): string {
  const words = key.replace(/_+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A nested reading on one line, keys unquoted and strings quoted so a string
 *  "3" and a number 3 stay apart. */
export function compact(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(compact).join(", ")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value)
      .map(([key, inner]) => `${key}: ${compact(inner)}`)
      .join(", ")}}`;
  }
  return JSON.stringify(value) ?? String(value);
}

/** A value the console has no format of its own for: scalars as they read,
 *  anything nested compactly in the mono face rather than as
 *  "[object Object]". */
export function plainValue(value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return DASH;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    return Number.isFinite(value) ? value.toLocaleString(undefined, { maximumFractionDigits: 3 }) : DASH;
  }
  if (typeof value === "string") return value;
  return <code className="mono node-records-json">{compact(value)}</code>;
}

/** A reading in its unit, to at most `decimals` places with trailing zeros
 *  dropped: "0.5 s", "60 km", "30°". */
export function measure(value: number | null | undefined, unit: string, decimals: number): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  const number = value.toLocaleString(undefined, { maximumFractionDigits: decimals });
  return unit === "°" ? `${number}°` : `${number} ${unit}`;
}

/** How long a node has been up. formatDuration's hours stop reading well past
 *  a day ("250.0h"), and a node is meant to run for weeks, so days take over
 *  there. */
export function formatUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
  if (seconds < 86400) return formatDuration(seconds);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  return hours ? `${days}d ${hours}h` : `${days}d`;
}
