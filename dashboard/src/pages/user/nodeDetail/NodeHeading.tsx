import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { LocationPrivacyBadge } from "../../../components/LocationPrivacy";
import { RetnodeLink } from "../../../components/RetnodeLink";

interface Props {
  /** The owner's label or the node's own name, else its ref. */
  title: string;
  /** The node's private id, for its owner alone: RetnodeLink links the title
   *  to the node's own site with it, and renders plain text without it. */
  nodeId?: string | null;
  synthetic?: boolean;
  isPrivate?: boolean;
  subtitle?: ReactNode;
}

/** The page's header: the way back, then the node by name. The Back button
 *  sits beside the heading rather than in it, so the heading reads as the
 *  node's name alone. nodeDetail.css wraps the row on a phone. */
export function NodeHeading({ title, nodeId = null, synthetic, isPrivate, subtitle }: Props) {
  const navigate = useNavigate();
  return (
    <div className="page-header node-detail-header">
      <div className="node-detail-heading">
        <button type="button" className="btn btn-secondary btn-sm node-detail-back" onClick={() => navigate(-1)}>
          <span aria-hidden="true">←</span> Back
        </button>
        <h1 className="node-detail-title">
          <span className="node-detail-name">
            <RetnodeLink nodeId={nodeId} synthetic={synthetic}>
              {title}
            </RetnodeLink>
          </span>
          <LocationPrivacyBadge isPrivate={isPrivate} />
        </h1>
      </div>
      {subtitle && <p>{subtitle}</p>}
    </div>
  );
}
