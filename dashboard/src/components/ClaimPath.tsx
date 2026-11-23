import type { ReactNode } from "react";

import "./ownerScope.css";

/** How a node joins an account. The one wording of it, so no page describes a
 *  path that does not exist. */
export const CLAIM_PATH =
  "To connect a node, enter your email address in its setup and we will send you a link. Click it and the node joins your account.";

/**
 * The claim path as a note under a page header or inside a card. Information,
 * not an alert: nothing has gone wrong, so nothing is announced. `lead` is the
 * page's own sentence before it, such as why the owner view is empty.
 */
export function ClaimPathNote({ lead }: { lead?: ReactNode }) {
  return (
    <div className="claim-path">
      <svg
        className="claim-path-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        <circle cx="12" cy="12" r="10" />
        <line x1="12" y1="16" x2="12" y2="12" />
        <line x1="12" y1="8" x2="12.01" y2="8" />
      </svg>
      <p className="claim-path-text">
        {lead ? (
          <>
            <span className="claim-path-lead">{lead}</span>{" "}
          </>
        ) : null}
        {CLAIM_PATH}
      </p>
    </div>
  );
}
