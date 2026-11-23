import { useState } from "react";
import { Link } from "react-router-dom";

import { ClaimPathNote } from "../../../components/ClaimPath";
import { DataTable } from "../../../components/DataTable";
import { LocationPrivacyBadge } from "../../../components/LocationPrivacy";
import { FetchNotice, Notice } from "../../../components/Notice";
import { Pager, clampPage } from "../../../components/Pager";
import { StatCard } from "../../../components/StatCard";
import type { OwnedNodes } from "../../../hooks/useOwnedNodes";
import type { Polled } from "../../../hooks/usePolling";
import type { LeaderboardRow, OwnedNode } from "../../../types";
import { DASH, formatAvailability } from "../../../utils/format";
import { detectionCount, isOnline, ownedNodePage, shortRef } from "../../../utils/nodes";
import {
  count,
  displayName,
  formatShare,
  formatTrust,
  overlapReading,
  overlapsInvolving,
  shareOf,
  unrankedReason,
  type NodeSummary,
  type OverlapReading,
  type Overlaps,
  type Standings,
} from "./figures";
import { SHARED_POINTS } from "./NetworkContribution";
import { NodeTag } from "./NodeTag";

const PAGE_SIZE = 25;

/** What a failure notice needs of a source. */
export type FetchState = Pick<Polled<unknown>, "error" | "updatedAt" | "refresh">;

/** Where one of the page's sources stands: its first answer on the way, that
 *  answer failed with nothing loaded, or an answer to show. */
export type SourceState = "loading" | "failed" | "ready";

interface Props {
  owned: OwnedNodes;
  /** The owner's true figures by node_ref, private nodes included; null until
   *  this owner's have landed. */
  own: ReadonlyMap<string, NodeSummary> | null;
  ownState: SourceState;
  ownFetch: FetchState;
  /** The network's detections, each node counted once; null without them. */
  networkTotal: number | null;
  networkState: SourceState;
  standings: Standings | null;
  standingsState: SourceState;
  overlaps: Overlaps | null;
  overlapsState: SourceState;
  names: ReadonlyMap<string, string>;
}

/** What the signed-in owner's nodes add to the network. The page shows it
 *  while their list is loading, failed or holds a node; with none, it shows
 *  the claim path instead. */
export function YourContribution(props: Props) {
  const { owned } = props;
  const heading = (
    <h2 className="section-title">
      Your contribution
      {owned.status === "ready" && owned.nodes.length > 0 && <span className="card-note">{count(owned.nodes.length, "node")}</span>}
    </h2>
  );

  if (owned.status === "loading") {
    return (
      <>
        {heading}
        <p className="section-caption" role="status">
          Loading your nodes…
        </p>
      </>
    );
  }
  if (owned.status !== "ready") {
    return (
      <>
        {heading}
        <Notice onRetry={owned.refresh}>
          Could not load your nodes: {owned.error?.message ?? "no answer"}. The network below is shown without them.
        </Notice>
      </>
    );
  }

  if (owned.nodes.length === 0) {
    return (
      <>
        {heading}
        <ClaimPathNote lead="No node is on your account yet." />
      </>
    );
  }

  return (
    <>
      {heading}
      {/* A failed refresh keeps the last list, so the section carries on. */}
      {owned.error && (
        <Notice onRetry={owned.refresh}>
          Could not refresh your nodes: {owned.error.message}. Showing the list loaded earlier.
        </Notice>
      )}
      <FetchNotice polled={props.ownFetch} what="your nodes' figures" />
      {props.ownState === "loading" && (
        <p className="section-caption" role="status">
          {"Loading your nodes' figures…"}
        </p>
      )}
      {props.own && (
        <Figures
          nodes={owned.nodes}
          own={props.own}
          networkTotal={props.networkTotal}
          networkState={props.networkState}
          standings={props.standings}
          standingsState={props.standingsState}
          overlaps={props.overlaps}
        />
      )}
      <YourOverlaps
        nodes={owned.nodes}
        refs={owned.refs}
        overlaps={props.overlaps}
        overlapsState={props.overlapsState}
        names={props.names}
      />
    </>
  );
}

/** One owned node's figures, all from the owner's own analytics but its rank. */
interface Row {
  node: OwnedNode;
  summary: NodeSummary | null;
  detections: number | null;
  share: number | null;
  standing: LeaderboardRow | null;
}

const AVAILABILITY = <span title="Share of the last 7 days' minutes it delivered in">Availability (7 d)</span>;
const HEADERS = ["Node", "Detections", "Share", "Rank", AVAILABILITY, "Trust", "Overlaps"];

function Figures({
  nodes,
  own,
  networkTotal,
  networkState,
  standings,
  standingsState,
  overlaps,
}: {
  nodes: readonly OwnedNode[];
  own: ReadonlyMap<string, NodeSummary>;
  networkTotal: number | null;
  networkState: SourceState;
  standings: Standings | null;
  standingsState: SourceState;
  overlaps: Overlaps | null;
}) {
  const rows: Row[] = nodes
    .map((node) => {
      const summary = (node.node_ref && own.get(node.node_ref)) || null;
      const detections = summary ? detectionCount(summary) : null;
      return {
        node,
        summary,
        detections,
        share: detections === null ? null : shareOf(detections, networkTotal),
        standing: (node.node_ref && standings?.byRef.get(node.node_ref)) || null,
      };
    })
    // Most detections first; a node with no figures yet goes last.
    .sort(
      (a, b) => (b.detections ?? -1) - (a.detections ?? -1) || displayName(a.node).localeCompare(displayName(b.node)),
    );

  const yours = rows.reduce((s, row) => s + (row.detections ?? 0), 0);
  const online = nodes.filter((node) => isOnline(node.status)).length;
  const ranked = rows.flatMap((row) => (row.standing ? [row.standing] : []));
  const best = ranked.reduce<LeaderboardRow | null>((top, row) => (!top || row.rank < top.rank ? row : top), null);

  let shareSub: string;
  if (networkTotal !== null) shareSub = `of ${networkTotal.toLocaleString()} network detections`;
  else if (networkState === "loading") shareSub = "waiting for the network's figures";
  else shareSub = "the network's figures did not load";

  let rankedSub: string;
  if (standings) {
    rankedSub = best ? `best #${best.rank} of ${standings.of.toLocaleString()}` : "none on the leaderboard yet";
  } else if (standingsState === "loading") rankedSub = "waiting for the leaderboard";
  else rankedSub = "the leaderboard did not load";

  return (
    <>
      <div className="stats-grid contribution-stats">
        <StatCard
          label="Your Detections"
          value={yours.toLocaleString()}
          tone="accent"
          sub={`across ${count(nodes.length, "node")}`}
        />
        <StatCard label="Share of Network" value={formatShare(shareOf(yours, networkTotal))} tone="accent" sub={shareSub} />
        <StatCard
          label="Nodes Online"
          value={`${online} / ${nodes.length}`}
          tone={online === nodes.length ? "success" : "warning"}
          sub="connected now"
        />
        <StatCard label="Nodes Ranked" value={standings ? `${ranked.length} / ${nodes.length}` : DASH} sub={rankedSub} />
      </div>

      <div className="card contribution-listing contribution-pinned">
        <div className="card-header">
          <h3>Your nodes</h3>
          <span className="card-note">True figures, private nodes included</span>
        </div>
        <DataTable headers={HEADERS} count={rows.length}>
          {rows.map((row) => (
            <NodeRow key={row.node.node_id} row={row} standings={standings} overlaps={overlaps} />
          ))}
        </DataTable>
      </div>
    </>
  );
}

function NodeRow({ row, standings, overlaps }: { row: Row; standings: Standings | null; overlaps: Overlaps | null }) {
  const { node, summary, detections, share, standing } = row;
  const page = ownedNodePage(node);
  const name = displayName(node);
  return (
    <tr>
      <td>
        <span className="rank-name">
          {page ? <Link to={page}>{name}</Link> : name}
          <LocationPrivacyBadge isPrivate={node.location_private} />
        </span>
        {!summary && <div className="card-note">No analytics yet</div>}
      </td>
      <td>{detections === null ? DASH : detections.toLocaleString()}</td>
      <td>{detections === null ? DASH : formatShare(share)}</td>
      <td>
        <RankCell node={node} standing={standing} standings={standings} />
      </td>
      <td>{formatAvailability(summary?.metrics?.availability_7d)}</td>
      <td>{formatTrust(summary?.trust?.trust_score)}</td>
      <td>
        <OverlapCell reading={overlapReading(node, overlaps)} />
      </td>
    </tr>
  );
}

/** "#r of N" from the leaderboard; "Not ranked" and why for a node it leaves
 *  out; a dash while there is no leaderboard to read, which is not the same
 *  as being left off it. */
function RankCell({
  node,
  standing,
  standings,
}: {
  node: OwnedNode;
  standing: LeaderboardRow | null;
  standings: Standings | null;
}) {
  if (!standings) return <>{DASH}</>;
  if (standing) {
    return (
      <>
        <span className="contribution-rank">#{standing.rank}</span> of {standings.of.toLocaleString()}
      </>
    );
  }
  return (
    <>
      <div>Not ranked</div>
      <div className="card-note contribution-reason">{unrankedReason(node)}</div>
    </>
  );
}

function OverlapCell({ reading }: { reading: OverlapReading }) {
  switch (reading.kind) {
    case "count":
      return <>{reading.count.toLocaleString()}</>;
    case "private":
      return (
        <span className="muted" title="Coverage overlaps are published without private nodes">
          Not published
        </span>
      );
    case "not-computed":
      return (
        <span className="muted" title="The network holds no coverage geometry for this node yet">
          Not computed
        </span>
      );
    default:
      return <>{DASH}</>;
  }
}

/** Every published overlap one of the owner's nodes is part of, naming the
 *  node on the other side. */
function YourOverlaps({
  nodes,
  refs,
  overlaps,
  overlapsState,
  names,
}: {
  nodes: readonly OwnedNode[];
  refs: ReadonlySet<string>;
  overlaps: Overlaps | null;
  overlapsState: SourceState;
  names: ReadonlyMap<string, string>;
}) {
  const [page, setPage] = useState(0);
  const pairs = overlaps ? overlapsInvolving(overlaps.pairs, refs) : [];
  const totalPages = Math.ceil(pairs.length / PAGE_SIZE);
  const current = clampPage(page, totalPages);
  const paged = pairs.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
  const name = (ref: string) => names.get(ref) ?? shortRef(ref);

  const allPrivate = nodes.every((node) => node.location_private);
  const somePrivate = !allPrivate && nodes.some((node) => node.location_private);
  let empty: string;
  if (overlapsState === "failed") empty = "Coverage overlaps did not load";
  else if (allPrivate) empty = "Coverage overlaps are published without private nodes, so yours are not listed";
  else empty = "None of your nodes shares coverage with another node yet";

  return (
    <div className="card contribution-listing">
      <div className="card-header">
        <h3>Overlaps involving your nodes</h3>
        <div className="card-aside contribution-aside">
          {overlaps && <span className="card-note">{count(pairs.length, "pair")}</span>}
          {somePrivate && <span className="card-note">Private nodes are not listed</span>}
        </div>
      </div>
      <DataTable
        headers={["Your Node", "Overlaps With", SHARED_POINTS]}
        count={paged.length}
        loading={overlapsState === "loading"}
        empty={empty}
      >
        {paged.map(({ mine, other, pair }) => (
          <tr key={`${pair.node_a}|${pair.node_b}`}>
            <td>
              <NodeTag name={name(mine)} mine={false} />
            </td>
            <td>
              <NodeTag name={name(other)} mine={refs.has(other)} />
            </td>
            <td>{pair.grid_points.toLocaleString()}</td>
          </tr>
        ))}
      </DataTable>
      <Pager page={current} totalPages={totalPages} onPage={setPage} />
    </div>
  );
}
