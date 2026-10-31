import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts";
import { api } from "../../api/client";
import { ClaimPathNote } from "../../components/ClaimPath";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, Notice } from "../../components/Notice";
import { ScopeSwitch } from "../../components/ScopeSwitch";
import { StatCard } from "../../components/StatCard";
import { useOwnedNodes } from "../../hooks/useOwnedNodes";
import { usePolling } from "../../hooks/usePolling";
import { useScopeParam } from "../../hooks/useScopeParam";
import { anomalyColour, useChartTheme } from "../../utils/chartTheme";
import { AnomalyTypeBadge } from "./anomalies/AnomalyTypeBadge";
import { LiveOnYourNodes } from "./anomalies/LiveOnYourNodes";
import "./anomalies/anomalies.css";

interface AnomalyEvent {
  hex: string;
  reason: string;
  lat?: number;
  lon?: number;
  ts?: number;
  flagged_at?: string;
  object_type?: string;
}

interface GeoCluster {
  lat: number;
  lon: number;
  count: number;
  dominant_type: string;
}

interface AnomalyData {
  summary: {
    active_count: number;
    total_events: number;
    unique_hexes: number;
    most_common_type: string | null;
  };
  by_type: Record<string, number>;
  timeline: { ts: number; count: number }[];
  geographic_clusters: GeoCluster[];
  recent_events: AnomalyEvent[];
}

function formatTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDateTime(iso: string) {
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

/**
 * The network's anomalies, and for a signed-in owner the ones their own nodes
 * see. Owner scope goes only where it means something: the live anomalies,
 * taken from the owner's feed (/api/auth/me/aircraft, private nodes included),
 * behind a "My nodes / Whole network" switch. The timeline, the breakdown, the
 * hotspots and the event table come from the anomaly log, which records no
 * node, so they stay network-wide and say so. Signed out, and on the admin
 * console, the page is the network's alone, with no switch and no owner calls.
 */
export default function AnomalyPage() {
  const chart = useChartTheme();
  const owned = useOwnedNodes();
  const hasNodes = owned.status === "ready" && owned.nodes.length > 0;
  const [scope, setScope] = useScopeParam(hasNodes);
  const polled = usePolling<AnomalyData>(() => api.anomalies(), 10000);
  const { data, loading, updatedAt: lastUpdated } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const title = (
    <>
      <h1>Anomaly Monitor</h1>
      <p>Real-time anomaly detection metrics and event log</p>
    </>
  );
  const header = hasNodes ? (
    <div className="page-header anomaly-header">
      <div className="anomaly-header-text">{title}</div>
      <ScopeSwitch value={scope} onChange={setScope} label="Show anomalies from" mineCount={owned.nodes.length} />
    </div>
  ) : (
    <div className="page-header">{title}</div>
  );
  // The owner's list failing leaves the network's page, which needs no list,
  // so this is a warning above it rather than an error in its place. A failed
  // refresh is not repeated here: the last list still holds, and the feed and
  // the network data each say when they fail.
  const notice = (
    <>
      <FetchNotice polled={polled} what="anomaly data" />
      {owned.status === "error" && (
        <Notice onRetry={owned.refresh}>
          Could not load your nodes: {owned.error?.message}. Showing the whole network.
        </Notice>
      )}
    </>
  );
  const claimPath = owned.status === "ready" && owned.nodes.length === 0 && (
    <ClaimPathNote lead="No nodes on your account yet, so this page shows the whole network." />
  );
  // The owner's live section stands where the network's live summary would,
  // and holds that place while the owner's list is on its way.
  const ownerSection = scope === "mine" || owned.status === "loading";
  const live =
    scope === "mine" ? (
      <LiveOnYourNodes nodes={owned.nodes} />
    ) : owned.status === "loading" ? (
      <p className="section-caption" role="status">Looking up your nodes…</p>
    ) : null;

  if (!data) {
    return (
      <>
        {header}
        {notice}
        {claimPath}
        {live}
      </>
    );
  }

  const { summary, by_type, timeline, geographic_clusters, recent_events } = data;

  const typeData = Object.entries(by_type || {})
    .map(([name, count]) => ({ name, count: count as number }))
    .sort((a, b) => b.count - a.count);

  return (
    <>
      {header}
      {notice}
      {claimPath}
      {live}

      {/* Beside an owner's own section, or while one may yet appear: what
          follows is everyone's, and why. */}
      {(hasNodes || owned.status === "loading") && (
        <>
          <h2 className="section-title">Whole network</h2>
          <p className="section-caption">The anomaly log does not record which node saw an event.</p>
        </>
      )}

      {/* ── Stats Grid ──────────────────────────────────────── */}
      {!ownerSection && (
        <div className="stats-grid">
          <StatCard
            label="Active Anomalies"
            value={summary?.active_count ?? 0}
            tone="error"
            sub="currently flagged"
          />
          <StatCard
            label="Total Events"
            value={summary?.total_events ?? 0}
            tone="accent"
            sub="in anomaly log"
          />
          <StatCard
            label="Unique Aircraft"
            value={summary?.unique_hexes ?? 0}
            tone="warning"
            sub="distinct hex codes"
          />
          <StatCard
            label="Most Common Type"
            value={summary?.most_common_type?.replace(/_/g, " ") ?? "—"}
            size="small"
          />
        </div>
      )}

      {/* ── Charts Row ──────────────────────────────────────── */}
      <div className="grid-2">
        {/* Timeline */}
        <div className="card">
          <div className="card-header"><h3>Event Timeline (24h)</h3></div>
          <div className="card-body">
            <div className="chart-container">
              {timeline && timeline.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={timeline}>
                    <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                    <XAxis
                      dataKey="ts"
                      tickFormatter={formatTime}
                      stroke={chart.axis}
                      tick={{ fontSize: 11 }}
                    />
                    <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} allowDecimals={false} />
                    <Tooltip
                      labelFormatter={(v) => new Date((v as number) * 1000).toLocaleString()}
                      contentStyle={chart.tooltip}
                      cursor={chart.cursor}
                    />
                    <Area
                      type="monotone"
                      dataKey="count"
                      stroke={chart.series[0]}
                      fill={chart.series[0]}
                      fillOpacity={0.2}
                      strokeWidth={2}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <div className="empty-state">No timeline data</div>
              )}
            </div>
          </div>
        </div>

        {/* Type Breakdown */}
        <div className="card">
          <div className="card-header"><h3>Breakdown by Type</h3></div>
          <div className="card-body">
            <div className="chart-container">
              {typeData.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={typeData} layout="vertical" margin={{ left: 20 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                    <XAxis type="number" stroke={chart.axis} tick={{ fontSize: 11 }} allowDecimals={false} />
                    <YAxis
                      type="category"
                      dataKey="name"
                      stroke={chart.axis}
                      tick={{ fontSize: 11 }}
                      width={140}
                      tickFormatter={(v) => v.replace(/_/g, " ")}
                    />
                    <Tooltip contentStyle={chart.tooltip} cursor={chart.cursor} />
                    <Bar dataKey="count" radius={[0, 4, 4, 0]}>
                      {typeData.map((entry, i) => (
                        <Cell key={i} fill={anomalyColour(chart, entry.name)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="empty-state">No anomalies detected</div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ── Geographic Clusters ─────────────────────────────── */}
      {geographic_clusters && geographic_clusters.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Geographic Hotspots</h3>
            <span className="card-note">Grouped by 0.1° grid</span>
          </div>
          <DataTable
            headers={["Rank", "Location", "Events", "Dominant Type"]}
            count={Math.min(geographic_clusters.length, 20)}
          >
            {geographic_clusters.slice(0, 20).map((c, i) => (
              <tr key={i}>
                <td>#{i + 1}</td>
                <td className="mono">
                  {c.lat.toFixed(1)}, {c.lon.toFixed(1)}
                </td>
                <td><strong>{c.count}</strong></td>
                <td>
                  <AnomalyTypeBadge type={c.dominant_type} />
                </td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}

      {/* ── Recent Events Table ─────────────────────────────── */}
      <div className="card">
        <div className="card-header">
          <h3>Recent Anomaly Events</h3>
          <span className="card-aside">
            {/* The page's notice is a long scroll above this table. */}
            {polled.error && <span className="badge warning">stale</span>}
            <span className="card-note">
              {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : "Auto-refreshes every 10s"}
            </span>
          </span>
        </div>
        <DataTable
          headers={["Flagged At", "Hex", "Type", "Lat", "Lon", "Object"]}
          count={recent_events?.length ?? 0}
          empty="No anomaly events recorded yet"
        >
          {[...(recent_events || [])].reverse().map((ev: AnomalyEvent, i: number) => (
            <tr key={`${ev.hex}-${ev.flagged_at ?? i}`}>
              <td className="mono" style={{ whiteSpace: "nowrap" }}>
                {ev.flagged_at ? formatDateTime(ev.flagged_at) : "—"}
              </td>
              <td className="mono" style={{ fontWeight: 600 }}>{ev.hex}</td>
              <td>
                <AnomalyTypeBadge type={ev.reason} />
              </td>
              <td className="mono">{ev.lat?.toFixed(4)}</td>
              <td className="mono">{ev.lon?.toFixed(4)}</td>
              <td>{ev.object_type || "—"}</td>
            </tr>
          ))}
        </DataTable>
      </div>
    </>
  );
}
