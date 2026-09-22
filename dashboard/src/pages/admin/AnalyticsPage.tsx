import { useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, LineChart, Line, Legend,
} from "recharts";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager, clampPage } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { useChartTheme, seriesColour } from "../../utils/chartTheme";
import { detectionCount, shortRef } from "../../utils/nodes";

const TOP_N_CHART = 15;
const PAGE_SIZE = 25;

export default function AnalyticsPage() {
  const chart = useChartTheme();
  const [trend, setTrend] = useState([]);
  const [overlapPage, setOverlapPage] = useState(0);

  const polled = usePolling(async () => {
    const [a, o] = await Promise.all([api.analytics(), api.overlaps()]);
    return { analytics: a, overlaps: Array.isArray(o) ? o : o.overlaps || [] };
  }, 10000, "", (snapshot) => {
    const summaries: any[] = Object.values(snapshot.analytics?.nodes || {});
    const totalDet = summaries.reduce((s, n) => s + detectionCount(n), 0);
    setTrend((prev) => [
      ...prev,
      {
        time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
        detections: totalDet,
        nodes: summaries.length,
      },
    ].slice(-30));
  });
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const analytics = data?.analytics;
  const overlaps = data?.overlaps ?? [];
  // Node identity is the map key (node_ref).
  const nodeEntries: [string, any][] = Object.entries(analytics?.nodes || {});
  const summaries = nodeEntries.map(([, n]) => n);

  // Trust distribution — show top N by trust, sorted descending
  const allTrust = nodeEntries.map(([ref, n]) => ({
    name: shortRef(ref),
    trust: Math.round((n.trust?.trust_score || 0) * 100),
    reputation: Math.round((n.reputation?.reputation || 0) * 100),
  })).sort((a, b) => b.trust - a.trust);
  const trustData = allTrust.slice(0, TOP_N_CHART);

  // Detection share — top 10 + "Others" bucket
  const allDetections = nodeEntries.map(([ref, n]) => ({
    name: shortRef(ref),
    value: detectionCount(n),
  })).sort((a, b) => b.value - a.value);
  const topDet = allDetections.slice(0, 10);
  const othersValue = allDetections.slice(10).reduce((s, d) => s + d.value, 0);
  const detectionShare = [
    ...topDet.map((d, i) => ({ ...d, fill: seriesColour(chart, i) })),
    ...(othersValue > 0 ? [{ name: `Others (${allDetections.length - 10})`, value: othersValue, fill: chart.others }] : []),
  ];

  const totalDetections = summaries.reduce((s, n) => s + detectionCount(n), 0);
  const totalFrames = summaries.reduce((s, n) => s + (n.metrics?.total_frames || 0), 0);

  const overlapPages = Math.ceil(overlaps.length / PAGE_SIZE);
  const currentOverlapPage = clampPage(overlapPage, overlapPages);
  const pagedOverlaps = overlaps.slice(currentOverlapPage * PAGE_SIZE, (currentOverlapPage + 1) * PAGE_SIZE);

  const header = (
    <div className="page-header">
      <h1>Network Analytics</h1>
      <p>Aggregate performance metrics and analysis</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="analytics" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="analytics" />

      <div className="stats-grid">
        <StatCard label="Total Detections" value={totalDetections.toLocaleString()} tone="accent" />
        <StatCard label="Total Frames" value={totalFrames.toLocaleString()} />
        <StatCard label="Node Count" value={summaries.length} tone="success" />
        <StatCard label="Coverage Pairs" value={overlaps.length} tone="warning" />
      </div>

      {trend.length > 1 && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header">
            <h3>Detection Trend</h3>
            <span className="card-note">
              Updates every 10s
            </span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={trend}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="time" stroke={chart.axis} tick={{ fontSize: 10 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={chart.tooltip}
                  />
                  <Line type="monotone" dataKey="detections" stroke={chart.series[0]} strokeWidth={2} dot={false} name="Detections" />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}

      <div className="grid-2">
        {/* Trust & reputation bar chart — top N */}
        <div className="card">
          <div className="card-header">
            <h3>Trust & Reputation — Top {TOP_N_CHART}</h3>
            <span className="card-note">{summaries.length} total nodes</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={trustData}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="name" stroke={chart.axis} tick={{ fontSize: 9 }} interval={0} angle={-35} textAnchor="end" height={50} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} domain={[0, 100]} />
                  <Tooltip
                    contentStyle={chart.tooltip}
                  />
                  <Legend />
                  <Bar dataKey="trust" fill={chart.series[0]} name="Trust %" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="reputation" fill={chart.series[1]} name="Reputation %" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>

        {/* Detection share donut — top 10 + Others */}
        <div className="card">
          <div className="card-header">
            <h3>Detection Share — Top 10</h3>
            <span className="card-note">{totalDetections.toLocaleString()} total</span>
          </div>
          <div className="card-body">
            <div className="chart-container">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={detectionShare}
                    cx="50%"
                    cy="50%"
                    outerRadius={90}
                    innerRadius={50}
                    dataKey="value"
                    labelLine={false}
                  >
                    {detectionShare.map((entry, index) => (
                      <Cell key={index} fill={entry.fill} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={chart.tooltip}
                    formatter={(value, name) => [value.toLocaleString(), name]}
                  />
                  <Legend
                    layout="vertical"
                    align="right"
                    verticalAlign="middle"
                    iconSize={10}
                    wrapperStyle={{ fontSize: 11, lineHeight: "18px" }}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </div>

      {/* Cross-node analysis with pagination */}
      {overlaps.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Cross-Node Overlap Analysis</h3>
            <span className="card-note">
              {overlaps.length} pairs
            </span>
          </div>
          <DataTable
            headers={["Node A", "Node B", "Jaccard Index", "Shared Bins", "Status"]}
            count={pagedOverlaps.length}
          >
            {pagedOverlaps.map((o, i) => {
              const j = o.jaccard || o.overlap || 0;
              return (
                <tr key={currentOverlapPage * PAGE_SIZE + i}>
                  <td className="mono">{shortRef(o.node_a)}</td>
                  <td className="mono">{shortRef(o.node_b)}</td>
                  <td>{j.toFixed(3)}</td>
                  <td>{o.shared_bins || o.shared || "—"}</td>
                  <td>
                    <span className={`badge ${j > 0.3 ? "online" : j > 0.1 ? "warning" : "offline"}`}>
                      {j > 0.3 ? "Strong" : j > 0.1 ? "Partial" : "Weak"}
                    </span>
                  </td>
                </tr>
              );
            })}
          </DataTable>
          <Pager page={currentOverlapPage} totalPages={overlapPages} onPage={setOverlapPage} />
        </div>
      )}
    </>
  );
}
