/**
 * Readings every page takes from a node. Each has one definition here, so the
 * same node reads the same wherever it is listed.
 */

// Statuses that mean the server holds no live connection for the node: the
// owner's node list says `never_connected` of one it has no record for, and
// every feed says `disconnected` of one that dropped. Anything else, `active`
// or whatever a heartbeat reported, is a node that is there.
const ABSENT = new Set(["disconnected", "never_connected"]);

/** Whether a node's `status` says it is connected. A missing status reads as
 *  offline. */
export function isOnline(status: string | null | undefined): boolean {
  return Boolean(status) && !ABSENT.has(status);
}

/** The word for a node's liveness. A node the owner's list has no record of
 *  says so rather than reading as one that dropped. */
export function statusLabel(status: string | null | undefined): string {
  if (isOnline(status)) return "Online";
  return status === "never_connected" ? "Never connected" : "Offline";
}

/** How many detections a node's analytics summary has counted. The two
 *  counters live in blocks the analytics service builds independently, so
 *  either can be absent: the frame metrics are read first and the detection
 *  area stands in where they are missing or zero. */
export function detectionCount(summary): number {
  return summary?.metrics?.total_detections || summary?.detection_area?.n_detections || 0;
}
