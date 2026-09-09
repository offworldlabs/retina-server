import type { PositionStatus } from "../types";

const LABELS: Record<Exclude<PositionStatus, "positioned">, string> = {
  missing_both: "No position",
  missing_rx: "No receiver position",
  missing_tx: "No illuminator position",
};

// Shared with NodeDetailPage's banner (verbatim) and OverviewPage's section
// header (paraphrased there until it started drifting from this wording).
export const POSITION_STATUS_EXPLANATION =
  "Detections from this node are counted and archived. It needs a " +
  "position before they can be placed on the map or contribute to solves.";

/** Sits beside `status`, never inside it: liveness and position completeness
 *  are separate questions, and a node detecting without a position is healthy. */
export function PositionStatusBadge({ status }: { status: PositionStatus }) {
  const label = LABELS[status as keyof typeof LABELS];
  // Renders for a status in the label map (i.e. not "positioned"); anything
  // else, including an unexpected or absent value, renders nothing rather
  // than an empty chip.
  if (!label) return null;
  return (
    <span className="badge warning" style={{ marginLeft: 8 }} title={POSITION_STATUS_EXPLANATION}>
      {label}
    </span>
  );
}
