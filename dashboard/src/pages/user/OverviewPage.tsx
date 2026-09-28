import { Link, useNavigate } from "react-router-dom";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { formatAvailability, formatRelativeTime } from "../../utils/format";
import { useChartTheme } from "../../utils/chartTheme";
import { PositionStatusBadge, POSITION_STATUS_EXPLANATION } from "../../components/PositionStatusBadge";
import { LocationPrivacyBadge } from "../../components/LocationPrivacy";
import { StatusBadge } from "../../components/StatusBadge";
import { detectionCount, isOnline, ownedNodePage } from "../../utils/nodes";
import type { OwnedNode } from "../../types";

export default function OverviewPage() {
  const chart = useChartTheme();
  const navigate = useNavigate();
  const polled = usePolling(async () => {
    const [mine, analytics, aircraft] = await Promise.all([
      api.myNodes(), api.analytics(), api.myAircraft(),
    ]);
    // The owner's list is the page's scope. The analytics map is keyed on
    // node_ref, and the route adds a private node back to it for its owner.
    const analyticsByRef = analytics?.nodes || {};
    const nodes = (Array.isArray(mine) ? mine : []).map((node: OwnedNode) => ({
      ...node,
      _analytics: (node.node_ref && analyticsByRef[node.node_ref]) || {},
    }));
    return { nodes, aircraftCount: (aircraft?.aircraft || []).length };
  }, 15000);
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = data?.nodes ?? [];
  const aircraftCount = data?.aircraftCount ?? 0;
  const onlineCount = nodes.filter((n) => isOnline(n.status)).length;
  const needsAttention = nodes.filter((n) => n.position_status && n.position_status !== "positioned");
  const totalDetections = nodes.reduce((s, n) => s + detectionCount(n._analytics), 0);
  const totalTracks = nodes.reduce((s, n) => s + (n._analytics?.metrics?.total_tracks || 0), 0);

  // A dash would label two unnamed nodes' bars alike.
  const chartData = nodes.map((n, i) => ({
    name: n.name || n.node_ref || `Node ${i + 1}`,
    detections: detectionCount(n._analytics),
  }));

  const header = (
    <div className="page-header">
      <h1>My Nodes Overview</h1>
      <p>Monitor your passive radar nodes in real time</p>
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
        <div className="card">
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
                <tr
                  key={node.node_id}
                  style={page ? { cursor: "pointer" } : undefined}
                  onClick={page ? () => navigate(page) : undefined}
                >
                  <td style={{ color: page ? "var(--accent)" : undefined }}>
                    {node.name || node.node_ref || "—"}
                  </td>
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
          <h3>My Nodes</h3>
        </div>
        <div className="node-grid" style={{ padding: 16 }}>
          {nodes.map((node) => {
            const page = ownedNodePage(node);
            return (
              <div
                className="node-card"
                key={node.node_id}
                style={page ? undefined : { cursor: "default" }}
                onClick={page ? () => navigate(page) : undefined}
              >
                <div className="node-name">
                  <StatusBadge status={node.status} />
                  <LocationPrivacyBadge isPrivate={node.location_private} />
                  {node.name || node.node_ref || "—"}
                </div>
                <div className="node-meta">
                  <span className="meta-label">Detections</span>
                  <span>{detectionCount(node._analytics).toLocaleString()}</span>
                  <span className="meta-label">Tracks</span>
                  <span>{node._analytics?.metrics?.total_tracks || 0}</span>
                  <span className="meta-label">Availability</span>
                  <span>{formatAvailability(node._analytics?.metrics?.availability_7d)}</span>
                  <span className="meta-label">Avg SNR</span>
                  <span>{(node._analytics?.metrics?.avg_snr || 0).toFixed(1)} dB</span>
                  <span className="meta-label">Heartbeat</span>
                  <span>{formatRelativeTime(node.last_heartbeat)}</span>
                  <span className="meta-label">Config</span>
                  <span className="mono">{node.config_hash ? node.config_hash.slice(0, 8) : "—"}</span>
                </div>
              </div>
            );
          })}
          {nodes.length === 0 && (
            <div className="empty-state">
              No nodes yet. <Link to="/onboarding">Connect your node</Link> to see it here.
            </div>
          )}
        </div>
      </div>
    </>
  );
}
