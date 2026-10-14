import { useEffect, useRef, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { HttpError } from "@retina/shared";
import { FetchNotice, Notice } from "../../components/Notice";
import { NodeOwnerRecords } from "../../components/nodeOwner/NodeOwnerRecords";
import { NodeOwnerSettings } from "../../components/nodeOwner/NodeOwnerSettings";
import { useFetch } from "../../hooks/usePolling";
import type { OwnedNode } from "../../types";
import { loadNode, type NodeView } from "./nodeDetail/loadNode";
import { NodeFigures, NoFiguresYet } from "./nodeDetail/NodeFigures";
import { NodeHeading } from "./nodeDetail/NodeHeading";
import { OwnershipCard, ReleasedCard, type Release } from "./nodeDetail/OwnershipCard";
import { PositionWarning } from "./nodeDetail/PositionWarning";
import { OwnerRfCard, PublicRfCard } from "./nodeDetail/RfConfigCard";
import "./nodeDetail/nodeDetail.css";

export default function NodeDetailPage() {
  const { nodeId } = useParams();
  // A route change is a new identity, including pending reads, an edit in
  // flight and a release still completing for the previous node.
  return <NodeDetail key={nodeId} param={nodeId ?? ""} />;
}

function NodeDetail({ param }: { param: string }) {
  // Whether the page last showed the node as the caller's and private, for
  // every later read to keep its ref off the public routes once the owner's
  // list no longer names it. Read when a read starts, never during render.
  const wasPrivate = useRef(false);
  // Its owner reads the node from the owner routes, anyone else from the
  // public ones; loadNode decides which, and keeps them apart.
  const polled = useFetch(() => loadNode(param, wasPrivate.current), param);

  // The node as the server answered the owner's last change. It stands until
  // the page reads the node again, which supersedes it.
  const [edited, setEdited] = useState<OwnedNode | null>(null);
  const [editedOver, setEditedOver] = useState(polled.data);
  if (polled.data !== editedOver) {
    setEditedOver(polled.data);
    setEdited(null);
  }
  // Bumped by each change, so the records fetch again and the history shows
  // the entry the change recorded.
  const [recordsKey, setRecordsKey] = useState(0);
  // How the node left the account in this visit: released here, or found gone
  // when a release or a change was answered 404. A public node is then read
  // again as anyone would see it, under the note that says so. A private one
  // has no public page, and the note says all there is.
  const [release, setRelease] = useState<Release | null>(null);

  const shown = polled.data?.kind === "owner" ? (edited ?? polled.data.node) : null;
  const shownPrivate = shown ? shown.location_private : polled.data?.kind === "gone";
  useEffect(() => {
    wasPrivate.current = shownPrivate;
  }, [shownPrivate]);

  function leave(outcome: Release) {
    setRelease(outcome);
    if (!wasPrivate.current) polled.refresh();
  }

  if (polled.loading) return <div className="empty-state">Loading…</div>;

  // After a release, an owner's reading is one from before it, still on hand:
  // its true positions, its unfuzzed figures and its records are no longer the
  // caller's to see. A public node shows nothing of it while the page reads
  // it again. A private one is gone, with nothing to read.
  let view: NodeView | null = polled.data;
  if (release && view?.kind === "owner") view = shownPrivate ? { kind: "gone" } : null;

  // Four slots, in this order whatever the page is showing, so the ownership
  // card stays the same card from the release to the public reading after it.
  let heading: ReactNode;
  let notices: ReactNode = null;
  let body: ReactNode = null;
  let ownership: ReactNode = release ? <ReleasedCard release={release} /> : null;

  if (view?.kind === "owner") {
    const node = edited ?? view.node;
    const title = node.name || node.node_ref || param;
    // The ref the map and the leaderboard know it by, where a name titles it.
    const ref = node.node_ref && node.node_ref !== title ? node.node_ref : null;
    heading = (
      <NodeHeading
        title={title}
        nodeId={node.node_id}
        synthetic={node.is_synthetic}
        isPrivate={node.location_private}
        subtitle={<>Your node{ref && <>, <span className="mono">{ref}</span></>}: its metrics, configuration and records.</>}
      />
    );
    notices = (
      <>
        <FetchNotice polled={polled} what="this node" />
        <PositionWarning status={node.position_status} />
        {view.analyticsError && (
          <Notice onRetry={polled.refresh}>Could not load this node&rsquo;s metrics: {view.analyticsError.message}</Notice>
        )}
      </>
    );
    body = (
      <>
        {view.analytics ? <NodeFigures analytics={view.analytics} /> : !view.analyticsError && <NoFiguresYet />}
        <OwnerRfCard node={node} />
        <NodeOwnerSettings
          node={node}
          onSaved={(updated) => {
            setEdited(updated);
            setRecordsKey((key) => key + 1);
          }}
          onGone={() => leave("gone")}
        />
        <NodeOwnerRecords nodeId={node.node_id} refreshKey={recordsKey} onGone={() => leave("gone")} />
      </>
    );
    ownership = <OwnershipCard node={node} nodeRef={param} onReleased={leave} />;
  } else if (view?.kind === "public" && view.analytics) {
    heading = (
      <NodeHeading
        title={view.analytics.node_ref || param}
        synthetic={view.nodeInfo?.is_synthetic}
        subtitle="Detailed metrics and trust analysis"
      />
    );
    notices = (
      <>
        <FetchNotice polled={polled} what="this node" />
        {view.ownershipUnknown && (
          <Notice onRetry={polled.refresh}>
            Could not check whether this node is yours: {view.ownershipUnknown.message}. This page shows what anyone
            can see.
          </Notice>
        )}
        <PositionWarning status={view.nodeInfo?.position_status} />
      </>
    );
    body = (
      <>
        <NodeFigures analytics={view.analytics} />
        {view.nodeInfo && <PublicRfCard nodeInfo={view.nodeInfo} />}
      </>
    );
  } else if (view?.kind === "gone") {
    // The caller's private node, gone from their account in this visit or
    // just before it: the route's own identifier, and the note.
    heading = <NodeHeading title={param} />;
    ownership = <ReleasedCard release={release ?? "gone"} />;
  } else {
    // Nothing to name the page by but the route's own identifier, and the way
    // back stays. A 404 is no such node, or one whose owner keeps it private;
    // any other failure says what went wrong. Neither lets the previous
    // node's details stand in for it.
    heading = <NodeHeading title={param} />;
    // While the reading after a release is on its way, a failure from before
    // it says nothing about the node now.
    const error = release && polled.pending ? null : polled.error;
    const missing = !error || (error instanceof HttpError && error.status === 404);
    if (!missing) {
      notices = (
        <Notice tone="error" onRetry={polled.refresh}>
          Could not load this node: {error.message}
        </Notice>
      );
    } else if (!release) {
      body = <div className="empty-state">Node not found</div>;
    }
  }

  return (
    <>
      {heading}
      {notices}
      {body}
      {ownership}
    </>
  );
}
