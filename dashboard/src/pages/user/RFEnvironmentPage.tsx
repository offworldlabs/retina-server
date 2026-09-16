import { useEffect, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  LineChart, Line,
} from "recharts";
import { api } from "../../api/client";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { useChartTheme } from "../../utils/chartTheme";

export default function RFEnvironmentPage() {
  const chart = useChartTheme();
  const [selectedNode, setSelectedNode] = useState("");
  const [snrHistory, setSnrHistory] = useState([]);

  // Keyed on the selection, so changing it fetches at once and restarts the
  // schedule rather than waiting out the current interval.
  const { data, loading } = usePolling(async () => {
    const [n, a] = await Promise.all([api.nodes(), api.analytics()]);
    const nodeMap = n.nodes || {};
    const analyticsMap = a?.nodes || {};
    const nodeList = Object.entries(nodeMap).map(([id, info]: [string, any]) => ({
      node_id: id,
      ...info,
      _analytics: analyticsMap[id] || {},
    }));
    const sel = selectedNode || (nodeList[0]?.node_id);
    const snr = analyticsMap[sel]?.metrics?.avg_snr || 0;
    return {
      nodes: nodeList,
      selectionKey: selectedNode,
      sample: sel ? {
        time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
        snr: parseFloat(snr.toFixed(1)),
      } : null,
    };
  }, 5000, selectedNode);

  useEffect(() => {
    // usePolling retains the previous key's data while the new selection
    // loads. Neither its sample nor its default selection belongs to this key.
    if (!data || data.selectionKey !== selectedNode) return;
    if (!selectedNode && data.nodes.length > 0) setSelectedNode(data.nodes[0].node_id);
    if (data.sample) setSnrHistory((prev) => [...prev.slice(-30), data.sample]);
  }, [data, selectedNode]);

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = data?.nodes ?? [];
  const selected = nodes.find((n) => n.node_id === selectedNode) || nodes[0];
  const metrics = selected?._analytics?.metrics || {};
  const freq = selected?.frequency || selected?._analytics?.detection_area?.center_freq;
  const location = selected?.location || {};

  // Build frequency utilization chart from top 20 nodes by SNR
  const freqData = nodes.map((n) => ({
    name: (n.name || n.node_id || "").slice(-10),
    frequency: (n.frequency || n._analytics?.detection_area?.center_freq || 0) / 1e6,
    snr: n._analytics?.metrics?.avg_snr || 0,
  })).sort((a, b) => b.snr - a.snr).slice(0, 20);

  return (
    <>
      <div className="page-header">
        <h1>RF Environment</h1>
        <p>Noise floor, signal strengths, and frequency utilization</p>
      </div>

      <div style={{ marginBottom: 16, display: "flex", alignItems: "center", gap: 12 }}>
        <select
          value={selectedNode}
          onChange={(e) => { setSelectedNode(e.target.value); setSnrHistory([]); }}
          style={{
            padding: "8px 12px",
            borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border)",
            fontSize: 13,
            background: "var(--bg-input)",
            color: "var(--text-primary)",
            maxWidth: 300,
          }}
        >
          {nodes.map((n) => (
            <option key={n.node_id} value={n.node_id}>
              {(n.name || n.node_id || "").slice(-16)}
            </option>
          ))}
        </select>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{nodes.length} nodes</span>
      </div>

      <div className="stats-grid">
        <StatCard label="Average SNR" value={<>{(metrics.avg_snr || 0).toFixed(1)} dB</>} tone="accent" />
        <StatCard label="Frequency" value={freq ? `${(freq / 1e6).toFixed(1)} MHz` : "—"} tone="success" />
        <StatCard label="Total Frames" value={(metrics.total_frames || 0).toLocaleString()} />
        <StatCard
          label="Detections"
          value={(metrics.total_detections || 0).toLocaleString()}
          tone="warning"
        />
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header">
            <h3>SNR Trend (Live)</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Updates every 5s</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={snrHistory}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="time" stroke={chart.axis} tick={{ fontSize: 10 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={chart.tooltip} />
                  <Line type="monotone" dataKey="snr" stroke={chart.series[0]} strokeWidth={2} dot={false} name="Avg SNR (dB)" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Signal Strength — Top 20</h3>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{nodes.length} total nodes</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={freqData}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="name" stroke={chart.axis} tick={{ fontSize: 10 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={chart.tooltip} />
                  <Bar dataKey="snr" fill={chart.series[1]} name="Avg SNR (dB)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header"><h3>Node RF Details</h3></div>
        <div className="card-body">
          <table>
            <tbody>
              <tr><td style={{ color: "var(--text-muted)" }}>Node ID</td><td style={{ fontFamily: "monospace" }}>{selected?.node_id}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>Frequency</td><td>{freq ? `${(freq / 1e6).toFixed(3)} MHz` : "Not configured"}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>Average SNR</td><td>{(metrics.avg_snr || 0).toFixed(2)} dB</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>Total Frames Processed</td><td>{(metrics.total_frames || 0).toLocaleString()}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>Detection Rate</td><td>{metrics.total_frames ? ((metrics.total_detections / metrics.total_frames) * 100).toFixed(1) + "%" : "—"}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>RX Location</td><td>{location.rx_lat != null && location.rx_lon != null ? `${location.rx_lat.toFixed(4)}, ${location.rx_lon.toFixed(4)}` : "—"}</td></tr>
              <tr><td style={{ color: "var(--text-muted)" }}>TX Location</td><td>{location.tx_lat != null && location.tx_lon != null ? `${location.tx_lat.toFixed(4)}, ${location.tx_lon.toFixed(4)}` : "—"}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
