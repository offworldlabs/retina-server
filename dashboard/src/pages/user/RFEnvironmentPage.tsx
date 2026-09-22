import { useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  LineChart, Line,
} from "recharts";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { useChartTheme } from "../../utils/chartTheme";
import { formatMHz } from "../../utils/format";
import { detectionCount, shortRef } from "../../utils/nodes";

export default function RFEnvironmentPage() {
  const chart = useChartTheme();
  const [selectedNode, setSelectedNode] = useState("");
  const [snrHistory, setSnrHistory] = useState([]);

  // Keyed on the selection, so changing it fetches at once and restarts the
  // schedule rather than waiting out the current interval.
  const polled = usePolling(async () => {
    const [n, a] = await Promise.all([api.nodes(), api.analytics()]);
    const nodeMap = n.nodes || {};
    const analyticsMap = a?.nodes || {};
    // Both are keyed on node_ref, the only identity these public feeds carry.
    const nodeList = Object.entries(nodeMap).map(([ref, info]: [string, any]) => ({
      ...info,
      node_ref: ref,
      _analytics: analyticsMap[ref] || {},
    }));
    const sel = selectedNode || (nodeList[0]?.node_ref);
    const snr = analyticsMap[sel]?.metrics?.avg_snr || 0;
    return {
      nodes: nodeList,
      sample: sel ? {
        time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
        snr: parseFloat(snr.toFixed(1)),
      } : null,
    };
  }, 5000, selectedNode, (snapshot) => {
    if (!selectedNode && snapshot.nodes.length > 0) setSelectedNode(snapshot.nodes[0].node_ref);
    if (snapshot.sample) setSnrHistory((prev) => [...prev.slice(-30), snapshot.sample]);
  });
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = data?.nodes ?? [];
  const selected = nodes.find((n) => n.node_ref === selectedNode) || nodes[0];
  const metrics = selected?._analytics?.metrics || {};
  const detections = detectionCount(selected?._analytics);
  const freq = selected?.frequency || selected?._analytics?.detection_area?.center_freq;
  const location = selected?.location || {};

  // Build frequency utilization chart from top 20 nodes by SNR
  const freqData = nodes.map((n) => ({
    name: n.name || shortRef(n.node_ref),
    frequency: (n.frequency || n._analytics?.detection_area?.center_freq || 0) / 1e6,
    snr: n._analytics?.metrics?.avg_snr || 0,
  })).sort((a, b) => b.snr - a.snr).slice(0, 20);

  const header = (
    <div className="page-header">
      <h1>RF Environment</h1>
      <p>Noise floor, signal strengths, and frequency utilization</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="RF environment data" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="RF environment data" />

      <div className="toolbar">
        <select
          value={selectedNode}
          onChange={(e) => { setSelectedNode(e.target.value); setSnrHistory([]); }}
          className="input"
        >
          {nodes.map((n) => (
            <option key={n.node_ref} value={n.node_ref}>
              {n.name || shortRef(n.node_ref)}
            </option>
          ))}
        </select>
        <span className="card-note">{nodes.length} nodes</span>
      </div>

      <div className="stats-grid">
        <StatCard label="Average SNR" value={<>{(metrics.avg_snr || 0).toFixed(1)} dB</>} tone="accent" />
        <StatCard label="Frequency" value={formatMHz(freq)} tone="success" />
        <StatCard label="Total Frames" value={(metrics.total_frames || 0).toLocaleString()} />
        <StatCard
          label="Detections"
          value={detections.toLocaleString()}
          tone="warning"
        />
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header">
            <h3>SNR Trend (Live)</h3>
            <span className="card-note">Updates every 5s</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={snrHistory}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="time" stroke={chart.axis} tick={{ fontSize: 10 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={chart.tooltip} cursor={chart.cursor} />
                  <Line type="monotone" dataKey="snr" stroke={chart.series[0]} strokeWidth={2} dot={false} name="Avg SNR (dB)" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header">
            <h3>Signal Strength — Top 20</h3>
            <span className="card-note">{nodes.length} total nodes</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={freqData}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="name" stroke={chart.axis} tick={{ fontSize: 10 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip contentStyle={chart.tooltip} cursor={chart.cursor} />
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
          <table className="kv-table">
            <tbody>
              <tr><td>Node ref</td><td className="mono">{selected?.node_ref}</td></tr>
              <tr><td>Frequency</td><td>{formatMHz(freq)}</td></tr>
              <tr><td>Average SNR</td><td>{(metrics.avg_snr || 0).toFixed(2)} dB</td></tr>
              <tr><td>Total Frames Processed</td><td>{(metrics.total_frames || 0).toLocaleString()}</td></tr>
              <tr><td>Detection Rate</td><td>{metrics.total_frames ? ((detections / metrics.total_frames) * 100).toFixed(1) + "%" : "—"}</td></tr>
              <tr><td>RX Location</td><td>{location.rx_lat != null && location.rx_lon != null ? `${location.rx_lat.toFixed(4)}, ${location.rx_lon.toFixed(4)}` : "—"}</td></tr>
              <tr><td>TX Location</td><td>{location.tx_lat != null && location.tx_lon != null ? `${location.tx_lat.toFixed(4)}, ${location.tx_lon.toFixed(4)}` : "—"}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
