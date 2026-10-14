import { Notice } from "../../../components/Notice";
import { POSITION_STATUS_EXPLANATION } from "../../../components/PositionStatusBadge";
import type { PositionStatus } from "../../../types";

const FIX_HINT: Record<Exclude<PositionStatus, "positioned">, string> = {
  missing_rx: "Add its receiver position in the node configuration.",
  missing_tx: "Add its illuminator position in the node configuration.",
  missing_both: "Add its receiver and illuminator positions in the node configuration.",
};

/** Which end of the node's pair has no position, as a warning under the
 *  page's header. Position completeness is orthogonal to the node's liveness
 *  (`status`): a positionless node can be detecting and perfectly healthy, so
 *  this is a Notice the page carries on through. A status the console has no
 *  words for shows nothing rather than a warning it cannot explain. */
export function PositionWarning({ status }: { status: PositionStatus | null | undefined }) {
  const hint = status && Object.prototype.hasOwnProperty.call(FIX_HINT, status)
    ? FIX_HINT[status as keyof typeof FIX_HINT]
    : null;
  if (!hint) return null;
  return (
    <Notice>
      <strong>Position not configured.</strong> {POSITION_STATUS_EXPLANATION} {hint}
    </Notice>
  );
}
