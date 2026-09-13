import type { ReactNode } from "react";

// Every node serves its own site at <node id>.retnode.com.
const RETNODE_DOMAIN = "retnode.com";

// The id becomes a DNS label verbatim, so anything that is not one gets no
// link rather than a URL that cannot resolve.
const DNS_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;

export function retnodeUrl(nodeId: string): string | null {
  if (!nodeId || !DNS_LABEL.test(nodeId)) return null;
  return `https://${nodeId.toLowerCase()}.${RETNODE_DOMAIN}`;
}

const externalLinkIcon = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6" />
    <polyline points="15 3 21 3 21 9" />
    <line x1="10" y1="14" x2="21" y2="3" />
  </svg>
);

type Props = {
  nodeId: string;
  /** From the node payload's `is_synthetic`. Simulated and test nodes have no
   *  box behind them, so they get no link; an absent value links, since only a
   *  positive verdict is evidence there is nothing to open. */
  synthetic?: boolean;
  /** What to show, when that is not the id itself — a node's name, say. */
  children?: ReactNode;
};

/** The node's id, linked to its own site. Renders the label as plain text when
 *  there is nothing to open, so a caller can use it wherever an id appears.
 *  Swallows the click: the Nodes page wraps it in a card that navigates. */
export function RetnodeLink({ nodeId, synthetic, children }: Props) {
  const label = children ?? nodeId;
  const url = synthetic ? null : retnodeUrl(nodeId);
  if (!url) return <>{label}</>;
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="retnode-link"
      title={`Open ${nodeId}.${RETNODE_DOMAIN}`}
      onClick={(e) => e.stopPropagation()}
    >
      {label}
      {externalLinkIcon}
    </a>
  );
}
