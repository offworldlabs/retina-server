import { useState } from "react";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { MapContainer, TileLayer, CircleMarker, Popup } from "react-leaflet";
import { TILES } from "../map/utils/tiles";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager, clampPage } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { formatMHz, formatRelativeTime, formatUptime } from "../../utils/format";
import { useChartTheme } from "../../utils/chartTheme";
import { RetnodeLink } from "../../components/RetnodeLink";
import { StatusBadge } from "../../components/StatusBadge";
import { useNodeIds } from "../../components/useNodeIds";
import { detectionCount, isOnline, statusLabel } from "../../utils/nodes";

const PAGE_SIZE = 25;

export default function NetworkHealthPage() {
  const chart = useChartTheme();
  const [history, setHistory] = useState([]);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const idsByRef = useNodeIds();

  const polled = usePolling(async () => {
    // Fetch fleet dashboard and node data in parallel; if fleetDashboard fails
    // we still render the node list from nodes/analytics.
    const [d, a, n, an] = await Promise.all([
      api.fleetDashboard().catch(() => null), api.aircraft(), api.nodes(), api.analytics(),
    ]);
    const aircraft = a.aircraft || [];
    // api.nodes() returns {nodes: {node_ref: {...}, ...}, total, connected},
    // and analytics.nodes is {node_ref: {trust, metrics, detection_area,
    // reputation, ...}} — both public feeds, both keyed on the published
    // identity and carrying no node_id. useNodeIds supplies that.
    const nodeMap = n.nodes || {};
    const analyticsMap = an?.nodes || {};
    const nodes = Object.entries(nodeMap).map(([ref, info]: [string, any]) => ({
      ...info,
      node_ref: ref,
      _analytics: analyticsMap[ref] || {},
    }));
    return { dashboard: d, aircraft, nodes };
  }, 5000, "", (snapshot) => {
    // Only accepted polling snapshots may append history.
    setHistory((prev) => [
      ...prev,
      {
        time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
        aircraft: snapshot.aircraft.length,
        nodes: snapshot.nodes.length,
      },
    ].slice(-30));
  });
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const dashboard = data?.dashboard;
  const aircraft = data?.aircraft ?? [];
  const nodes = data?.nodes ?? [];
  const dashNodes = dashboard?.nodes || {}; // {total, active, synthetic, real}
  const tracks = dashboard?.pipeline || {};
  const coc = dashboard?.chain_of_custody || {};
  const onlineNodes = nodes.filter((n) => isOnline(n.status));

  const header = (
    <div className="page-header">
      <h1>Network Health</h1>
      <p>Real-time monitoring of the passive radar network</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="network health" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="network health" />

      <div className="stats-grid">
        <StatCard
          label="Nodes Online"
          value={<>{dashNodes.active ?? onlineNodes.length} / {dashNodes.total ?? nodes.length}</>}
          tone="accent"
        />
        <StatCard label="Aircraft Tracked" value={aircraft.length} tone="success" />
        <StatCard label="Active Tracks" value={tracks.active_tracks || 0} tone="warning" />
        <StatCard label="CoC Chains" value={coc.nodes_with_chains || 0} />
      </div>

      {/* Live trend chart */}
      {history.length > 1 && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header">
            <h3>Live Network Activity</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
              Updates every 5s
            </span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={history}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="time" stroke={chart.axis} tick={{ fontSize: 10 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={chart.tooltip}
                  />
                  <Area type="monotone" dataKey="aircraft" stroke={chart.series[0]} fill={chart.series[0]} fillOpacity={0.15} name="Aircraft" />
                  <Area type="monotone" dataKey="nodes" stroke={chart.series[1]} fill={chart.series[1]} fillOpacity={0.15} name="Nodes" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      {/* Node location map */}
      {(() => {
        const geoNodes = nodes.filter(
          (n) => n.location?.rx_lat != null && n.location?.rx_lon != null,
        );
        if (geoNodes.length === 0) return null;
        const avgLat = geoNodes.reduce((s, n) => s + n.location.rx_lat, 0) / geoNodes.length;
        const avgLon = geoNodes.reduce((s, n) => s + n.location.rx_lon, 0) / geoNodes.length;
        return (
          <div className="card" style={{ marginBottom: 24 }}>
            <div className="card-header">
              <h3>Node Map</h3>
              <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                {geoNodes.length} nodes with location
              </span>
            </div>
            <div className="card-body" style={{ padding: 0, height: 400 }}>
              <MapContainer center={[avgLat, avgLon]} zoom={5} style={{ height: "100%", width: "100%", borderRadius: "0 0 8px 8px" }}>
                <TileLayer
                  attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
                  url={TILES.osm}
                />
                {geoNodes.map((node) => {
                  const ref = node.node_ref;
                  const online = isOnline(node.status);
                  return (
                    <CircleMarker
                      key={ref}
                      center={[node.location.rx_lat, node.location.rx_lon]}
                      radius={7}
                      // Literals, not the status tokens: these are painted onto
                      // the OSM basemap, which stays light in both themes, so the
                      // dark ramp would read worse here rather than better.
                      fillColor={online ? "#10b981" : "#ef4444"}
                      color={online ? "#059669" : "#dc2626"}
                      weight={2}
                      fillOpacity={0.8}
                    >
                      <Popup>
                        <strong>{node.name || ref}</strong><br />
                        Ref: {ref}<br />
                        Node ID: {idsByRef?.[ref] ?? "—"}<br />
                        Status: {statusLabel(node.status)}<br />
                        {node.frequency ? `Freq: ${formatMHz(node.frequency)}` : ""}
                      </Popup>
                    </CircleMarker>
                  );
                })}
              </MapContainer>
            </div>
          </div>
        );
      })()}

      {/* Node status grid */}
      <div className="card">
        <div className="card-header">
          <h3>Node Status</h3>
          <input
            type="text"
            placeholder="Search nodes…"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(0); }}
            style={{
              padding: "4px 10px",
              borderRadius: 6,
              border: "1px solid var(--border)",
              background: "var(--bg-input)",
              color: "var(--text-primary)",
              fontSize: 12,
              width: 180,
            }}
          />
        </div>
        {(() => {
          // Either identifier finds a node: an operator arrives holding
          // whichever one their last conversation used.
          const filtered = search
            ? nodes.filter((n) =>
                [n.node_ref, idsByRef?.[n.node_ref], n.name]
                  .some((s) => (s || "").toLowerCase().includes(search.toLowerCase())),
              )
            : nodes;
          const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
          const current = clampPage(page, totalPages);
          const paged = filtered.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
          return (
            <>
              <DataTable
                headers={["Node ref", "Node ID", "Status", "Last Heartbeat", "Detections", "Avg SNR", "Trust", "Reputation", "Uptime"]}
                count={paged.length}
                empty="No nodes found"
              >
                {paged.map((node) => {
                  const ref = node.node_ref;
                  // The node's own site is named after the private id, so
                  // the ref is the label and the id is the destination.
                  const nodeId = idsByRef?.[ref] ?? null;
                  return (
                    <tr key={ref}>
                      <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--accent)" }}>
                        <RetnodeLink nodeId={nodeId} synthetic={node.is_synthetic}>
                          {ref}
                        </RetnodeLink>
                      </td>
                      <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--text-muted)" }}>
                        {nodeId ?? "—"}
                      </td>
                      <td>
                        <StatusBadge status={node.status} />
                      </td>
                      <td style={{ fontSize: 12, color: "var(--text-muted)" }}>
                        {formatRelativeTime(node.last_heartbeat)}
                      </td>
                      <td>{detectionCount(node._analytics).toLocaleString()}</td>
                      <td>{(node._analytics?.metrics?.avg_snr || 0).toFixed(1)} dB</td>
                      <td>{((node._analytics?.trust?.trust_score || 0) * 100).toFixed(0)}%</td>
                      <td>{((node._analytics?.reputation?.reputation || 0) * 100).toFixed(0)}%</td>
                      <td>{formatUptime(node._analytics?.metrics?.uptime_s || 0)}</td>
                    </tr>
                  );
                })}
              </DataTable>
              <Pager page={current} totalPages={totalPages} onPage={setPage} note={`${filtered.length} nodes`} />
            </>
          );
        })()}
      </div>
    </>
  );
}
