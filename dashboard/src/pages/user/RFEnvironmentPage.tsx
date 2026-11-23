import { useId, useMemo, useState, type ReactNode } from "react";

import { api } from "../../api/client";
import { ClaimPathNote } from "../../components/ClaimPath";
import { LocationPrivacyBadge } from "../../components/LocationPrivacy";
import { FetchNotice, Notice, nothingLoaded } from "../../components/Notice";
import { YoursBadge } from "../../components/YoursBadge";
import { useOwnedNodes } from "../../hooks/useOwnedNodes";
import { usePolling } from "../../hooks/usePolling";
import type { OwnerSummary, SnrSeries } from "../../types";
import { defaultChoice, findChoice, nodeChoices, type NodeChoice } from "./rf/choices";
import { fetchNetwork, NO_SAMPLES, recordSamples, type NetworkSnapshot, type SampleHistory } from "./rf/feeds";
import { NodeDetails, NodeStats, networkFigures, ownedFigures } from "./rf/NodeFigures";
import { LiveSnrCard, NetworkSnrCard, OwnedSnrCard } from "./rf/SnrCards";
import { useKeyedPoll, type KeyedPoll } from "./rf/useKeyedPoll";

import "./rf/rf.css";

/** The public feeds' refresh, and so how often the live trend is sampled. */
const NETWORK_REFRESH_MS = 5000;
/** The owner's analytics are recomputed every 30 s; asking sooner gets the same figures. */
const ANALYTICS_REFRESH_MS = 30_000;
/** The server keeps a series for a minute, so a sooner poll reads the same answer. */
const SERIES_REFRESH_MS = 60_000;
/** The owned trend's window, which the server takes as 1 to 72 hours. */
const SERIES_HOURS = 24;
/** The contract's bucket, which titles the card until a series names its own. */
const SERIES_BUCKET_S = 300;

const plural = (n: number, one: string, many: string) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

/** A source the page carries on without. With nothing loaded it says what is
 *  missing; with an earlier answer on screen, how old that answer is. */
function PartialNotice({ polled, what, without }: { polled: KeyedPoll<unknown>; what: string; without: string }) {
  if (!polled.error) return null;
  if (polled.updatedAt) return <FetchNotice polled={polled} what={what} />;
  return (
    <Notice onRetry={polled.refresh}>
      Could not load {what}: {polled.error.message}. {without}
    </Notice>
  );
}

function Option({ choice }: { choice: NodeChoice }) {
  return <option value={choice.value}>{choice.label}</option>;
}

export default function RFEnvironmentPage() {
  const owned = useOwnedNodes();
  const pickerId = useId();
  // The option value the viewer chose, or the page's own first choice once
  // made, so a refresh never moves the selection on its own.
  const [picked, setPicked] = useState<string | null>(null);
  // Every public node's average, sampled on every answer since the page
  // opened, so the trend of a node turned to later still runs from then.
  const [samples, setSamples] = useState<SampleHistory>(NO_SAMPLES);

  // Not keyed on the selection: every node is sampled whichever is shown.
  const network = usePolling<NetworkSnapshot>(fetchNetwork, NETWORK_REFRESH_MS, "", (snapshot) =>
    setSamples((history) => recordSamples(history, snapshot)),
  );

  const networkNodes = network.data?.nodes;
  const choices = useMemo(
    () => nodeChoices(owned.nodes, networkNodes ?? [], owned.refs),
    [owned.nodes, networkNodes, owned.refs],
  );
  // Which nodes are the owner's is not known until their list is in, and an
  // owner's first choice is one of their own: none is made before then.
  const ownedSettled = owned.status !== "loading";
  const chosen = findChoice(choices, picked) ?? (ownedSettled ? defaultChoice(choices) : null);
  // Pinned once made, and made again when the node chosen is no longer offered.
  if (chosen !== null && chosen.value !== picked) setPicked(chosen.value);

  // The owner routes are asked only while one of the owner's nodes is shown.
  const nodeId = chosen?.kind === "owned" ? chosen.node.node_id : null;
  const series = useKeyedPoll<SnrSeries>(
    nodeId === null ? null : { key: `${nodeId}/${SERIES_HOURS}`, ask: () => api.myNodeSnr(nodeId, SERIES_HOURS) },
    SERIES_REFRESH_MS,
  );
  const analytics = useKeyedPoll<OwnerSummary | null>(
    nodeId === null ? null : { key: nodeId, ask: () => api.myNodeAnalytics(nodeId) },
    ANALYTICS_REFRESH_MS,
  );

  const header = (
    <div className="page-header">
      <h1>RF Environment</h1>
      <p>Noise floor, signal strengths, and frequency utilization</p>
    </div>
  );
  const hasOwned = choices.owned.length > 0;

  // With nodes of their own the owner has a page to show before the network
  // answers; without, there is nothing to show until it does.
  if (!ownedSettled || (network.loading && !hasOwned)) {
    return (
      <>
        {header}
        <div className="empty-state">Loading…</div>
      </>
    );
  }

  let banner: ReactNode = null;
  if (owned.status === "error") {
    banner = (
      <Notice onRetry={owned.refresh}>
        Could not load your nodes: {owned.error?.message}. Showing the network&rsquo;s nodes.
      </Notice>
    );
  } else if (owned.status === "ready" && owned.nodes.length === 0) {
    banner = <ClaimPathNote lead="No nodes on your account yet, so this page shows the whole network." />;
  }

  const networkNotice =
    hasOwned && nothingLoaded(network) ? (
      <Notice onRetry={network.refresh}>
        Could not load the network&rsquo;s figures: {network.error?.message}. Showing your nodes only.
      </Notice>
    ) : (
      <FetchNotice polled={network} what="RF environment data" />
    );

  if (chosen === null) {
    return (
      <>
        {header}
        {banner}
        {networkNotice}
        {!nothingLoaded(network) && <div className="empty-state">No node publishes to the network yet.</div>}
      </>
    );
  }

  const figures =
    chosen.kind === "owned"
      ? ownedFigures(chosen.node, analytics.answer?.value, analytics.loading)
      : networkFigures(chosen.node);
  // The network's share is counted only once the network has answered: until
  // then it is unknown, not none.
  const yours = `${choices.owned.length.toLocaleString()} yours`;
  let count = plural(choices.network.length, "node", "nodes");
  if (hasOwned) count = networkNodes ? `${yours} · ${choices.network.length.toLocaleString()} on the network` : yours;

  return (
    <>
      {header}
      {banner}
      {networkNotice}
      {chosen.kind === "owned" && (
        <PartialNotice polled={analytics} what="this node's analytics" without="Its counts are not shown." />
      )}

      <div className="toolbar">
        <div className="rf-picker">
          <label htmlFor={pickerId}>Node</label>
          <select id={pickerId} className="input" value={chosen.value} onChange={(e) => setPicked(e.target.value)}>
            {hasOwned ? (
              <>
                <optgroup label="Your nodes">
                  {choices.owned.map((c) => <Option key={c.value} choice={c} />)}
                </optgroup>
                {choices.network.length > 0 && (
                  <optgroup label="Network">
                    {choices.network.map((c) => <Option key={c.value} choice={c} />)}
                  </optgroup>
                )}
              </>
            ) : (
              choices.network.map((c) => <Option key={c.value} choice={c} />)
            )}
          </select>
        </div>
        {/* The closed field shows the choice without its group, so the mark
            stands beside it. */}
        {chosen.kind === "owned" && (
          <span className="rf-tags">
            <YoursBadge />
            <LocationPrivacyBadge isPrivate={chosen.node.location_private} />
          </span>
        )}
        <span className="card-note rf-count">{count}</span>
      </div>

      <NodeStats figures={figures} />

      <div className="grid-2">
        {chosen.kind === "owned" ? (
          <OwnedSnrCard poll={series} hours={SERIES_HOURS} bucketS={SERIES_BUCKET_S} />
        ) : (
          <LiveSnrCard samples={samples.get(chosen.node.ref) ?? []} every={NETWORK_REFRESH_MS} />
        )}
        <NetworkSnrCard
          nodes={networkNodes ?? []}
          mine={owned.refs}
          loading={network.loading}
          failed={nothingLoaded(network)}
        />
      </div>

      <NodeDetails figures={figures} owned={chosen.kind === "owned"} />
    </>
  );
}
