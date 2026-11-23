import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";
import { CLAIM_PATH, ClaimPathNote } from "../../components/ClaimPath";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { formatAvailability, formatMHz, formatRelativeTime } from "../../utils/format";
import { useChartTheme } from "../../utils/chartTheme";
import { PositionStatusBadge, POSITION_STATUS_EXPLANATION } from "../../components/PositionStatusBadge";
import { LocationPrivacyBadge } from "../../components/LocationPrivacy";
import { StatusBadge } from "../../components/StatusBadge";
import { useAuth } from "../../context/AuthContext";
import { detectionCount, isOnline, ownedNodePage } from "../../utils/nodes";
import { describeFailure } from "../../utils/requestFailure";
import type { MyClaim, OwnedNode } from "../../types";
import "./OpenClaims.css";

/** Where one row's resend stands. A row with nothing recorded is idle. `gone`
 *  is the server saying the node is no longer waiting on this account. */
type Resend = { state: "sending" | "sent" | "gone" } | { state: "failed"; message: string };

/** What a 429 on a resend means: its limit is per node and can last up to an
 *  hour, which the sign-in card's "wait a minute" would understate. */
const TOO_MANY_LINKS = "Too many links have been asked for. Try again later.";

/** When a link lapses, as a time on the viewer's clock, with the date once it
 *  is not today's. */
function lapses(at: number): string {
  const when = new Date(at * 1000);
  const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return when.toDateString() === new Date().toDateString()
    ? time
    : `${when.toLocaleDateString([], { day: "numeric", month: "short" })}, ${time}`;
}

function linkState(claim: MyClaim): { text: string; live: boolean } {
  if (claim.pending && claim.expires_at !== null && claim.expires_at * 1000 > Date.now()) {
    return { text: `Link is live until ${lapses(claim.expires_at)}`, live: true };
  }
  return { text: "Link has expired", live: false };
}

/** The account's open claims, or null when they could not be read. A claim
 *  list that fails says nothing about the nodes beside it, so it is caught
 *  here and never reaches the page's own fetch. */
async function readClaims(): Promise<MyClaim[] | null> {
  try {
    const list = await api.myClaims();
    return Array.isArray(list) ? list : [];
  } catch (err) {
    console.error(err);
    return null;
  }
}

/** Nodes that were given one of the owner's addresses and are not theirs yet,
 *  each with the means to have the mail sent again. Nothing at all when there
 *  are none. `onSent` is told after a mail is queued, so the list is read
 *  anew: a send replaces the outstanding link and its time. It is told too
 *  when the server says the claim is gone, so the list drops the row. */
function OpenClaims({ claims, onSent }: { claims: MyClaim[]; onSent: () => void }) {
  const [resends, setResends] = useState<Record<string, Resend>>({});
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function resend(nodeRef: string) {
    setResends((all) => ({ ...all, [nodeRef]: { state: "sending" } }));
    try {
      await api.resendMyClaim(nodeRef);
      if (!mounted.current) return;
      setResends((all) => ({ ...all, [nodeRef]: { state: "sent" } }));
      onSent();
    } catch (e) {
      if (!mounted.current) return;
      const failure = describeFailure(e, { throttled: TOO_MANY_LINKS });
      // 404: claimed, released or withdrawn since the list was read. Said
      // below the list, which the refresh is about to take the row out of.
      if (failure.status === 404) {
        setResends((all) => ({ ...all, [nodeRef]: { state: "gone" } }));
        onSent();
        return;
      }
      setResends((all) => ({ ...all, [nodeRef]: { state: "failed", message: failure.message } }));
    }
  }

  const gone = Object.keys(resends).filter((nodeRef) => resends[nodeRef].state === "gone");

  if (claims.length === 0 && gone.length === 0) return null;

  return (
    <div className="card open-claims-card">
      <div className="card-header">
        <h3>Waiting for you</h3>
        <span className="card-note">Nodes that were given your address and are not on your account yet.</span>
      </div>
      <ul className="open-claims">
        {claims.map((claim) => {
          const row = resends[claim.node_ref];
          const link = linkState(claim);
          return (
            <li className="open-claim" key={claim.node_ref}>
              <div className="open-claim-main">
                <div className="open-claim-ref mono">{claim.node_ref}</div>
                <div className="open-claim-note">Given {claim.email}</div>
                <div className={link.live ? "open-claim-note live" : "open-claim-note lapsed"}>{link.text}</div>
                {row?.state === "sent" && (
                  <div className="open-claim-note sent" role="status">
                    Sent. The link lasts fifteen minutes.
                  </div>
                )}
                {row?.state === "failed" && (
                  <div className="open-claim-note failed" role="alert">
                    {row.message}
                  </div>
                )}
              </div>
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                disabled={row?.state === "sending"}
                onClick={() => resend(claim.node_ref)}
              >
                {row?.state === "sending" ? "Sending…" : "Send the link again"}
              </button>
            </li>
          );
        })}
      </ul>
      {gone.map((nodeRef) => (
        <div className="open-claim" key={nodeRef}>
          <div className="open-claim-main">
            <div className="open-claim-note" role="status">
              Node {nodeRef} is no longer waiting for you.
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/** A node's name, linked to its page where it has one. `name` is the owner's
 *  label where they set one. The link is the one control in its card or row,
 *  and the stylesheet stretches it over the whole of either: a pointer can
 *  click anywhere on it, and Tab stops once per node. A node with no page is
 *  named in plain text and its card or row is not a control at all. */
function NodeName({ node, page }: { node: OwnedNode; page: string | null }) {
  const name = node.name || node.node_ref || "—";
  if (!page) return <>{name}</>;
  return <Link to={page} className="node-link">{name}</Link>;
}

export default function OverviewPage() {
  const { polledRadarRegistration } = useAuth();
  const chart = useChartTheme();
  // The last claim list that was read, kept for a poll whose read fails.
  const lastClaims = useRef<MyClaim[]>([]);
  const polled = usePolling(async () => {
    // The owner's own analytics, not the network's. The page is the caller's
    // nodes alone, and this route has each of them as it is, nothing fuzzed
    // and a private one included; nor is every node's summary fetched every
    // 15 s to read a few of them.
    const [mine, analytics, aircraft, read] = await Promise.all([
      api.myNodes(), api.myAnalytics(), api.myAircraft(), readClaims(),
    ]);
    const claims = read ?? lastClaims.current;
    lastClaims.current = claims;
    // The owner's list is the page's scope, joined to its figures on node_ref.
    const summaries = analytics?.nodes ?? {};
    const nodes = (Array.isArray(mine) ? mine : []).map((node) => ({
      ...node,
      summary: (node.node_ref && summaries[node.node_ref]) || null,
    }));
    return { nodes, claims, aircraftCount: (aircraft?.aircraft || []).length };
  }, 15000);
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = data?.nodes ?? [];
  const claims = data?.claims ?? [];
  const aircraftCount = data?.aircraftCount ?? 0;
  const onlineCount = nodes.filter((n) => isOnline(n.status)).length;
  const needsAttention = nodes.filter((n) => n.position_status && n.position_status !== "positioned");
  const totalDetections = nodes.reduce((s, n) => s + detectionCount(n.summary), 0);
  const totalTracks = nodes.reduce((s, n) => s + (n.summary?.metrics?.total_tracks || 0), 0);

  // A dash would label two unnamed nodes' bars alike.
  const chartData = nodes.map((n, i) => ({
    name: n.name || n.node_ref || `Node ${i + 1}`,
    detections: detectionCount(n.summary),
  }));

  const header = (
    <div className="page-header">
      <h1>My Nodes</h1>
      <p>{CLAIM_PATH}</p>
      {polledRadarRegistration && (
        <p>
          Running stock 30hours/blah2? <Link to="/radars/new">Add a stock blah2 radar</Link> by its address.
        </p>
      )}
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="your nodes" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="your nodes" />

      <OpenClaims claims={claims} onSent={polled.refresh} />

      <div className="stats-grid">
        <StatCard label="Nodes Online" value={<>{onlineCount} / {nodes.length}</>} tone="accent" />
        <StatCard label="Live Aircraft" value={aircraftCount.toLocaleString()} tone="success" />
        <StatCard label="Frame Detections" value={totalDetections.toLocaleString()} />
        <StatCard label="Tracks" value={totalTracks.toLocaleString()} />
      </div>

      {needsAttention.length > 0 && (
        <div className="card linked-rows">
          <div className="card-header">
            <h3>Needs Attention</h3>
            <span className="card-note">
              {POSITION_STATUS_EXPLANATION}
            </span>
          </div>
          <DataTable headers={["Node", "Position"]} count={needsAttention.length}>
            {needsAttention.map((node) => {
              // Listed even with no page to link to: this is the only place
              // its owner is told.
              const page = ownedNodePage(node);
              return (
                <tr key={node.node_id} className={page ? "linked" : undefined}>
                  <td><NodeName node={node} page={page} /></td>
                  <td><PositionStatusBadge status={node.position_status} /></td>
                </tr>
              );
            })}
          </DataTable>
        </div>
      )}

      {/* One node's bar would only repeat its card. */}
      {chartData.length > 1 && (
        <div className="card">
          <div className="card-header">
            <h3>Detections by Node</h3>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="name" stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={chart.tooltip}
                    cursor={chart.cursor}
                  />
                  <Bar dataKey="detections" fill={chart.series[0]} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-header">
          <h3>Nodes</h3>
        </div>
        {nodes.length > 0 ? (
          <div className="node-grid">
            {nodes.map((node) => {
              const page = ownedNodePage(node);
              return (
                <div className={page ? "node-card linked" : "node-card unlinked"} key={node.node_id}>
                  <div className="node-name">
                    <StatusBadge status={node.status} />
                    <LocationPrivacyBadge isPrivate={node.location_private} />
                    <NodeName node={node} page={page} />
                  </div>
                  {/* The values an owner copies are .selectable: they stay
                      above the card's link, so a drag selects them. */}
                  <div className="node-meta">
                    {/* The handle the map and leaderboard know it by, where the
                        card is titled otherwise: by the owner's label or the
                        node's own name. */}
                    {node.node_ref && node.name && node.name !== node.node_ref && (
                      <>
                        <span className="meta-label">Ref</span>
                        <span className="mono selectable">{node.node_ref}</span>
                      </>
                    )}
                    <span className="meta-label">Frequency</span>
                    <span>{formatMHz(node.frequency)}</span>
                    <span className="meta-label">Location</span>
                    <span className="selectable">
                      {node.rx_lat != null && node.rx_lon != null
                        ? `${node.rx_lat.toFixed(3)}, ${node.rx_lon.toFixed(3)}`
                        : "—"}
                    </span>
                    <span className="meta-label">Detections</span>
                    <span>{detectionCount(node.summary).toLocaleString()}</span>
                    <span className="meta-label">Tracks</span>
                    <span>{node.summary?.metrics?.total_tracks || 0}</span>
                    <span className="meta-label">Availability</span>
                    <span>{formatAvailability(node.summary?.metrics?.availability_7d)}</span>
                    <span className="meta-label">Avg SNR</span>
                    <span>{(node.summary?.metrics?.avg_snr || 0).toFixed(1)} dB</span>
                    <span className="meta-label">Heartbeat</span>
                    <span>{formatRelativeTime(node.last_heartbeat)}</span>
                    <span className="meta-label">Config</span>
                    <span className="mono selectable">{node.config_hash ? node.config_hash.slice(0, 8) : "—"}</span>
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="card-body">
            <ClaimPathNote lead="No nodes on your account yet." />
          </div>
        )}
      </div>
    </>
  );
}
