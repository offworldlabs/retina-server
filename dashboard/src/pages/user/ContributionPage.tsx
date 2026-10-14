import { api } from "../../api/client";
import { FetchNotice, Notice } from "../../components/Notice";
import { useAuth } from "../../context/AuthContext";
import { useOwnedNodes } from "../../hooks/useOwnedNodes";
import { usePolling, type Polled } from "../../hooks/usePolling";
import type { OwnerAnalytics } from "../../types";
import {
  networkNodes,
  nodeNames,
  readOverlaps,
  readStandings,
  totalDetections,
  type NodeSummary,
} from "./contribution/figures";
import { NetworkContribution } from "./contribution/NetworkContribution";
import { YourContribution, type SourceState } from "./contribution/YourContribution";
import "./contribution/contribution.css";

const REFRESH_MS = 30_000;

/** The owner's figures with the owner they were asked for, so the previous
 *  account's are never shown as the next one's. */
interface OwnAnswer {
  owner: string;
  summaries: Map<string, NodeSummary>;
}

// Nothing to ask for: settles at once, so no request leaves the page.
const nothing = () => Promise.resolve(null);

/** Where a source stands: an answer to show, its first on the way, or failed
 *  with nothing to show. A failed refresh keeps its last answer. */
function stateOf(polled: Pick<Polled<unknown>, "data" | "loading">): SourceState {
  if (polled.data) return "ready";
  return polled.loading ? "loading" : "failed";
}

/** A source the page carries on without. With nothing loaded it says what is
 *  missing; with an earlier answer on screen, how old that answer is. */
function PartialNotice({ polled, what, without }: { polled: Polled<unknown>; what: string; without: string }) {
  if (!polled.error) return null;
  if (polled.updatedAt) return <FetchNotice polled={polled} what={what} />;
  return (
    <Notice onRetry={polled.refresh}>
      Could not load {what}: {polled.error.message}. {without}
    </Notice>
  );
}

export default function ContributionPage() {
  const { user } = useAuth();
  const owned = useOwnedNodes();
  const signedIn = owned.status !== "signed-out";

  const network = usePolling(() => api.analytics(), REFRESH_MS);
  const overlapsPolled = usePolling(() => api.overlaps(), REFRESH_MS);
  const leaderboard = usePolling(() => api.leaderboard(), REFRESH_MS);

  // Only an owner with a node has figures to ask for.
  const owner = owned.status === "ready" && owned.nodes.length > 0 && user ? String(user.id) : null;
  const ownPolled = usePolling<OwnAnswer | null>(
    owner === null
      ? nothing
      : () =>
          api.myAnalytics().then((answer: OwnerAnalytics) => ({
            owner,
            summaries: new Map<string, NodeSummary>(Object.entries(answer?.nodes ?? {})),
          })),
    owner === null ? 0 : REFRESH_MS,
    owner === null ? "" : `user:${owner}`,
  );
  const ownAnswer = owner !== null && ownPolled.data?.owner === owner ? ownPolled.data : null;
  const own = ownAnswer ? ownAnswer.summaries : null;
  // Not while pending: an error left from the previous account, or from the
  // attempt a retry is replacing, is not this request's answer.
  const ownFetch = {
    error: ownPolled.pending ? null : ownPolled.error,
    updatedAt: ownAnswer ? ownPolled.updatedAt : null,
    refresh: ownPolled.refresh,
  };
  let ownState: SourceState = "loading";
  if (own) ownState = "ready";
  else if (ownFetch.error) ownState = "failed";

  const networkState = stateOf(network);
  const nodes = network.data ? networkNodes(network.data.nodes, own) : null;
  const networkTotal = nodes ? totalDetections(nodes.values()) : null;
  const standings = leaderboard.data ? readStandings(leaderboard.data) : null;
  const overlaps = overlapsPolled.data ? readOverlaps(overlapsPolled.data) : null;
  const names = nodeNames(standings?.rows ?? [], owned.nodes);

  return (
    <>
      <div className="page-header">
        <h1>Contribution</h1>
        <p>
          {signedIn
            ? "What your nodes add to the passive radar network, and where they stand in it"
            : "Detections, coverage and standings across the passive radar network"}
        </p>
      </div>

      {signedIn && (
        <YourContribution
          owned={owned}
          own={own}
          ownState={ownState}
          ownFetch={ownFetch}
          networkTotal={networkTotal}
          networkState={networkState}
          standings={standings}
          standingsState={stateOf(leaderboard)}
          overlaps={overlaps}
          overlapsState={stateOf(overlapsPolled)}
          names={names}
        />
      )}

      {signedIn && <h2 className="section-title">Network</h2>}
      <FetchNotice polled={network} what="network contribution" />
      <PartialNotice polled={leaderboard} what="the leaderboard" without="Ranks are not shown." />
      <PartialNotice polled={overlapsPolled} what="coverage overlaps" without="Overlaps are not shown." />
      <NetworkContribution
        nodes={nodes}
        loading={networkState === "loading"}
        overlaps={overlaps}
        standings={standings}
        mine={owned.refs}
        names={names}
      />
    </>
  );
}
