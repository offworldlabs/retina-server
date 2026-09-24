import { useNavigate } from "react-router-dom";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
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
import { detectionCount, isOnline } from "../../utils/nodes";

export default function OverviewPage() {
  const chart = useChartTheme();
  const navigate = useNavigate();
  const polled = usePolling(async () => {
    // myNodes fails soft: it is only needed for the needs-attention list, and
    // an unauthenticated view of this page must still render the rest.
    const [n, a, ac, mine] = await Promise.all([
      api.nodes(), api.analytics(), api.aircraft(), api.myNodes().catch(() => []),
    ]);
    // Both are dicts keyed on node_ref, and the values carry no identifier of
    // their own, so the key is the identity.
    const nodeMap = n.nodes || {};
    const analyticsMap = a?.nodes || {};
    const nodes = Object.entries(nodeMap).map(([ref, info]: [string, any]) => ({
      node_ref: ref,
      ...info,
      _analytics: analyticsMap[ref] || {},
    }));
    return {
      nodes,
      myNodes: Array.isArray(mine) ? mine : [],
      aircraftCount: (ac.aircraft || []).length,
    };
  }, 15000);
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodeList = data?.nodes ?? [];
  const myNodes = data?.myNodes ?? [];
  const aircraftCount = data?.aircraftCount ?? 0;
  const onlineCount = nodeList.filter((n) => isOnline(n.status)).length;
  // Merged with the owner's own nodes, because /api/radar/nodes drops private
  // ones: a private node with no position would otherwise appear nowhere its
  // owner looks, and this list is the only place they are told.
  // Joined on node_ref, the one key space both sides share: /api/auth/me/nodes
  // also carries node_id, and keying on that would list every node twice.
  // A node with no ref is on no public surface, so its node_id cannot collide.
  const keyOf = (n: any) => n.node_ref || n.node_id || n.id;
  const byRef = new Map<string, any>(nodeList.map((n) => [keyOf(n), n]));
  for (const n of myNodes) {
    const key = keyOf(n);
    if (!byRef.has(key)) byRef.set(key, n);
  }
  const needsAttention = [...byRef.values()].filter((n) => n.position_status && n.position_status !== "positioned");
  // Same reason the merge exists at all: a private node is dropped from
  // /api/radar/nodes, so its owner would otherwise not find it in the grid
  // below — the one place they are told it is private.
  const myNodeCards = [...byRef.values()];
  // Keyed the same way the merge above is, not on node_id: a public node
  // reaches the grid from the ref-keyed listing and only its owner's copy
  // carries the flag, so the two have to meet in one key space.
  const privateByKey = new Map<string, boolean>(
    myNodes.map((n) => [keyOf(n), !!n.location_private]),
  );
  const totalFrameDetections = nodeList.reduce((s, n) => s + detectionCount(n._analytics), 0);

  // Build a simple detection-over-index chart from node data
  const chartData = nodeList.map((n, i) => ({
    name: n.name || n.node_ref || `Node ${i + 1}`,
    detections: detectionCount(n._analytics),
    tracks: n._analytics?.metrics?.total_tracks || 0,
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
        <StatCard label="Nodes Online" value={<>{onlineCount} / {nodeList.length}</>} tone="accent" />
        <StatCard label="Live Aircraft" value={aircraftCount.toLocaleString()} tone="success" />
        <StatCard label="Frame Detections" value={totalFrameDetections.toLocaleString()} />
        <StatCard label="Network Nodes" value={nodeList.length} tone="warning" />
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
              // The detail page addresses the node on public routes, which
              // take the ref.  A node with no ref is on no public surface,
              // so its row is still listed (this is the only place its
              // owner is told) but it is not a link to a 404.
              const ref = node.node_ref;
              return (
                <tr
                  key={ref || node.node_id || node.id}
                  style={ref ? { cursor: "pointer" } : undefined}
                  onClick={ref ? () => navigate(`/nodes/${ref}`) : undefined}
                >
                  <td style={{ color: ref ? "var(--accent)" : undefined }}>
                    {node.name || ref || "—"}
                  </td>
                  <td><PositionStatusBadge status={node.position_status} /></td>
                </tr>
              );
            })}
          </DataTable>
        </div>
      )}

      {chartData.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Detections by Node</h3>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="name" stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={chart.tooltip}
                    cursor={chart.cursor}
                  />
                  <Area
                    type="monotone"
                    dataKey="detections"
                    stroke={chart.series[0]}
                    fill={chart.series[0]}
                    fillOpacity={0.15}
                  />
                </AreaChart>
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
          {myNodeCards.map((node) => {
            // The card links to the detail page, which addresses a node on the
            // public routes and so takes the ref.  A node with no ref is on no
            // public surface: its card is still shown — this list is the only
            // place its owner is told about it — but it is not a link to a 404.
            const ref = node.node_ref;
            const id = keyOf(node);
            return (
              <div
                className="node-card"
                key={id}
                style={ref ? undefined : { cursor: "default" }}
                onClick={ref ? () => navigate(`/nodes/${ref}`) : undefined}
              >
                <div className="node-name">
                  <StatusBadge status={node.status} />
                  <LocationPrivacyBadge isPrivate={privateByKey.get(id)} />
                  {node.name || ref || "—"}
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
          {myNodeCards.length === 0 && (
            <div className="empty-state">No nodes connected yet</div>
          )}
        </div>
      </div>
    </>
  );
}
