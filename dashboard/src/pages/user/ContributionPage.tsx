import { useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager, clampPage } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { useChartTheme } from "../../utils/chartTheme";

const PAGE_SIZE = 25;

export default function ContributionPage() {
  const chart = useChartTheme();
  const [overlapPage, setOverlapPage] = useState(0);
  const polled = usePolling(async () => {
    const [a, o, lb] = await Promise.all([api.analytics(), api.overlaps(), api.leaderboard().catch(() => [])]);
    return {
      analytics: a,
      overlaps: Array.isArray(o) ? o : o.overlaps || [],
      leaderboard: Array.isArray(lb) ? lb : lb.leaderboard || [],
    };
  }, 30000);
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const analytics = data?.analytics;
  const overlaps = data?.overlaps ?? [];
  const leaderboard = data?.leaderboard ?? [];

  // analytics.nodes is a dict {node_ref: summary} from the backend; the ref
  // is the map key, values no longer carry node_id.
  const rawNodes = analytics?.nodes || {};
  const summaries = Array.isArray(rawNodes) ? rawNodes : Object.values(rawNodes);
  const nodeEntries: [string, any][] = Array.isArray(rawNodes)
    ? rawNodes.map((n) => [n.node_ref || "", n])
    : Object.entries(rawNodes);

  // Build contribution chart — top 20 by detections
  const chartDataAll = nodeEntries.map(([ref, n]) => ({
    name: (ref || n.name || "").slice(-8),
    detections: n.metrics?.total_detections || n.detection_area?.n_detections || 0,
    trust: Math.round((n.trust?.trust_score || 0) * 100),
  })).sort((a, b) => b.detections - a.detections);
  const chartData = chartDataAll.slice(0, 20);

  const totalDetections = summaries.reduce((s, n) => s + (n.metrics?.total_detections || n.detection_area?.n_detections || 0), 0);
  const avgTrust = summaries.length
    ? summaries.reduce((s, n) => s + (n.trust?.trust_score || 0), 0) / summaries.length
    : 0;

  const header = (
    <div className="page-header">
      <h1>Network Contribution</h1>
      <p>Your contribution metrics across the passive radar network</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="network contribution" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="network contribution" />

      <div className="stats-grid">
        <StatCard label="Network Detections" value={totalDetections.toLocaleString()} tone="accent" />
        <StatCard label="Avg Trust Score" value={<>{(avgTrust * 100).toFixed(1)}%</>} tone="success" />
        <StatCard label="Active Nodes" value={summaries.length} />
        <StatCard label="Correlation Pairs" value={overlaps.length} tone="warning" />
      </div>

      {chartData.length > 0 && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header">
            <h3>Detections per Node — Top 20</h3>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{summaries.length} total nodes</span>
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
                  />
                  <Bar dataKey="detections" fill={chart.series[0]} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      {overlaps.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Coverage Overlaps</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{overlaps.length} pairs</span>
          </div>
          {(() => {
            const totalPages = Math.ceil(overlaps.length / PAGE_SIZE);
            const current = clampPage(overlapPage, totalPages);
            const paged = overlaps.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
            return (
              <>
                <DataTable headers={["Node A", "Node B", "Jaccard Index", "Shared Bins"]} count={paged.length}>
                  {paged.map((o, i) => (
                    <tr key={current * PAGE_SIZE + i}>
                      <td style={{ fontFamily: "monospace" }}>{(o.node_a || "").slice(-8)}</td>
                      <td style={{ fontFamily: "monospace" }}>{(o.node_b || "").slice(-8)}</td>
                      <td>{(o.jaccard || o.overlap || 0).toFixed(3)}</td>
                      <td>{o.shared_bins || o.shared || "—"}</td>
                    </tr>
                  ))}
                </DataTable>
                <Pager page={current} totalPages={totalPages} onPage={setOverlapPage} />
              </>
            );
          })()}
        </div>
      )}

      {leaderboard.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-header">
            <h3>Network Rankings</h3>
          </div>
          <DataTable headers={["#", "Node", "Detections", "Trust"]} count={Math.min(leaderboard.length, 10)}>
            {leaderboard.slice(0, 10).map((entry, i) => (
              <tr key={entry.node_ref || i}>
                <td style={{ fontWeight: 600, color: i < 3 ? "var(--accent)" : "var(--text-muted)" }}>{i + 1}</td>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{(entry.node_ref || "").slice(-12)}</td>
                <td>{(entry.detections || 0).toLocaleString()}</td>
                <td>{((entry.trust || 0) * 100).toFixed(0)}%</td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}
    </>
  );
}
