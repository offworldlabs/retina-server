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
import type { OwnedNode } from "../../types";

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
  const polled = usePolling(async () => {
    // The owner's own analytics, not the network's. The page is the caller's
    // nodes alone, and this route has each of them as it is, nothing fuzzed
    // and a private one included; nor is every node's summary fetched every
    // 15 s to read a few of them.
    const [mine, analytics, aircraft] = await Promise.all([
      api.myNodes(), api.myAnalytics(), api.myAircraft(),
    ]);
    // The owner's list is the page's scope, joined to its figures on node_ref.
    const summaries = analytics?.nodes ?? {};
    const nodes = (Array.isArray(mine) ? mine : []).map((node) => ({
      ...node,
      summary: (node.node_ref && summaries[node.node_ref]) || null,
    }));
    return { nodes, aircraftCount: (aircraft?.aircraft || []).length };
  }, 15000);
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = data?.nodes ?? [];
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
