const TONES = new Map<string, string>([
  ["info", "info"],
  ["warning", "warning"],
  ["error", "offline"],
  ["critical", "offline"],
]);

/** The `.badge` class for an event's severity. A severity the map does not
 *  know reads as informational. */
export function severityTone(severity: string | null | undefined): string {
  return TONES.get(severity) ?? "info";
}
