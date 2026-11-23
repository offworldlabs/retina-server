import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { HttpError } from "@retina/shared";
import { api } from "../../../api/client";
import { Notice } from "../../../components/Notice";
import type { OwnedNode } from "../../../types";

/** How the node left the owner's account: released from this card, or found
 *  gone already, when the server answers the release 404 because the node was
 *  released or reassigned elsewhere. Either way it is no longer theirs. */
export type Release = "released" | "gone";

interface Props {
  node: OwnedNode;
  /** Where its radar page is: the route's ref, should the node carry none. */
  nodeRef: string;
  onReleased: (release: Release) => void;
}

/** The owner's hold on the node, and handing it back. Releasing is here rather
 *  than on a list page because it wants the node named in front of it, and it
 *  asks first, as removing a radar does on its own page. */
export function OwnershipCard({ node, nodeRef, onReleased }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [releasing, setReleasing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (node.polled) {
    // Removing a polled radar retires it rather than handing it back, and its
    // own page says so.
    return (
      <div className="card node-ownership">
        <div className="card-header"><h3>Ownership</h3></div>
        <div className="card-body stack">
          <p>
            You registered this stock blah2 radar by its address. Change the address or remove it
            on <Link to={`/radars/${node.node_ref || nodeRef}`}>its radar page</Link>.
          </p>
        </div>
      </div>
    );
  }

  async function release() {
    setReleasing(true);
    setError(null);
    try {
      await api.releaseNode(node.node_id);
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) {
        onReleased("gone");
        return;
      }
      setError((e as Error).message || "Could not release this node. Try again.");
      setReleasing(false);
      return;
    }
    // The page takes this card down with the rest of the owner's view.
    onReleased("released");
  }

  return (
    <div className="card node-ownership">
      <div className="card-header"><h3>Ownership</h3></div>
      <div className="card-body stack">
        <p>
          {node.claimed_with
            ? <>Claimed with <strong>{node.claimed_with}</strong>.</>
            : <>This node was assigned to you rather than claimed with an email address.</>}
        </p>
        <p className="muted">
          Releasing hands it back so its next owner can claim it. You stop seeing its unpublished
          data straight away, and the node learns within a minute.
        </p>
        {error && <Notice tone="error">{error}</Notice>}
        {confirming ? (
          <>
            <p><strong>Release this node?</strong> Whoever claims it next becomes its owner.</p>
            <div className="btn-row">
              <button type="button" className="btn btn-danger" onClick={release} disabled={releasing}>
                {releasing ? "Releasing…" : "Yes, release it"}
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                onClick={() => {
                  setConfirming(false);
                  setError(null);
                }}
                disabled={releasing}
              >
                Keep it
              </button>
            </div>
          </>
        ) : (
          <div className="btn-row">
            <button type="button" className="btn btn-secondary" onClick={() => setConfirming(true)}>
              Release this node
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** What the Ownership card says once the node has left the account, in place
 *  of the controls the server now answers 404 for. */
export function ReleasedCard({ release }: { release: Release }) {
  // The button that was pressed has gone with the controls, so focus comes
  // here: a keyboard keeps its place, and a screen reader reads the outcome.
  const note = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    note.current?.focus();
  }, []);
  return (
    <div className="card node-ownership">
      <div className="card-header"><h3>Ownership</h3></div>
      <div className="card-body stack">
        <p ref={note} tabIndex={-1} className="card-note node-release-note" role="status">
          {release === "released" && "Released. "}This node is no longer on your account.{" "}
          <Link to="/overview">Go to My Nodes</Link>
        </p>
      </div>
    </div>
  );
}
