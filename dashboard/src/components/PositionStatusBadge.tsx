import type { PositionStatus } from "../types";

const LABELS: Record<Exclude<PositionStatus, "positioned">, string> = {
  missing_both: "No position",
  missing_rx: "No receiver position",
  missing_tx: "No illuminator position",
};

const TITLE =
  "This node's detections are being recorded normally. It just needs a " +
  "position before it can appear on the map or contribute to solves.";

/** Sits beside `status`, never inside it: liveness and position completeness
 *  are separate questions, and a node detecting without a position is healthy. */
export function PositionStatusBadge({ status }: { status: PositionStatus }) {
  if (status === "positioned") return null;
  return (
    <span className="badge warning" style={{ marginLeft: 8 }} title={TITLE}>
      {LABELS[status]}
    </span>
  );
}
