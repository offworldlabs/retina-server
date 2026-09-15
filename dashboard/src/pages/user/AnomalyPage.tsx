import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts";
import { api } from "../../api/client";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { useChartTheme } from "../../utils/chartTheme";
import { useResolvedTheme, type Theme } from "../../context/ThemeContext";

interface AnomalyEvent {
  hex: string;
  reason: string;
  lat?: number;
  lon?: number;
  ts?: number;
  flagged_at?: string;
  object_type?: string;
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
  geographic_clusters: { lat: number; lon: number; count: number; dominant_type: string }[];
  recent_events: AnomalyEvent[];
}

/**
 * A domain palette, not the status ramp: these hues say which kind of anomaly,
 * not how bad it is. Dark keeps every hue and takes it one step lighter, the
 * same move the chart series makes, so a type is recognisably itself in either
 * theme.
 */
const TYPE_COLOURS = {
  light: {
    supersonic: "#ef4444",
    instant_acceleration: "#f97316",
    instant_direction_change: "#eab308",
    sustained_orbit: "#8b5cf6",
    position_mismatch: "#3b82f6",
    identity_swap: "#ec4899",
    altitude_jump: "#14b8a6",
    anomalous_behavior: "#6b7280",
  },
  dark: {
    supersonic: "#f87171",
    instant_acceleration: "#fb923c",
    instant_direction_change: "#facc15",
    sustained_orbit: "#a78bfa",
    position_mismatch: "#60a5fa",
    identity_swap: "#f472b6",
    altitude_jump: "#2dd4bf",
    anomalous_behavior: "#9ca3af",
  },
} as const satisfies Record<Theme, Record<string, string>>;

/** For a reason the backend added before this map did. */
const TYPE_FALLBACK: Record<Theme, string> = { light: "#6b7280", dark: "#9ca3af" };

/** Ink for a badge whose fill is one of the colours above. White is unreadable
 *  on the lighter dark variants. */
const BADGE_INK: Record<Theme, string> = { light: "#ffffff", dark: "#0b1220" };

function typeColour(theme: Theme, type: string | undefined): string {
  const palette: Record<string, string> = TYPE_COLOURS[theme];
  // hasOwnProperty, not a plain lookup: these keys come from the backend, and
  // `constructor` or `toString` would otherwise resolve up the prototype chain
  // and hand Recharts a function as a colour.
  return type && Object.prototype.hasOwnProperty.call(palette, type)
    ? palette[type]
    : TYPE_FALLBACK[theme];
}

function formatTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatDateTime(iso: string) {
  return new Date(iso).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export default function AnomalyPage() {
  const chart = useChartTheme();
  const theme = useResolvedTheme();
  const { data, loading, error, updatedAt: lastUpdated } = usePolling<AnomalyData>(
    () => api.anomalies(),
    10000,
  );
  const stale = error !== null;

  if (loading) return <div className="empty-state">Loading…</div>;
  if (!data) return <div className="empty-state">Failed to load anomaly data</div>;

  const { summary, by_type, timeline, geographic_clusters, recent_events } = data;

  const typeData = Object.entries(by_type || {})
    .map(([name, count]) => ({ name, count: count as number }))
    .sort((a, b) => b.count - a.count);

  return (
    <>
      <div className="page-header">
        <h1>Anomaly Monitor</h1>
        <p>Real-time anomaly detection metrics and event log</p>
      </div>

      {/* ── Stats Grid ──────────────────────────────────────── */}
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

      {/* ── Charts Row ──────────────────────────────────────── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        {/* Timeline */}
        <div className="card">
          <div className="card-header"><h3>Event Timeline (24h)</h3></div>
          <div style={{ padding: 16, height: 260 }}>
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
                  />
                  <Area
                    type="monotone"
                    dataKey="count"
                    stroke={typeColour(theme, "supersonic")}
                    fill={typeColour(theme, "supersonic")}
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

        {/* Type Breakdown */}
        <div className="card">
          <div className="card-header"><h3>Breakdown by Type</h3></div>
          <div style={{ padding: 16, height: 260 }}>
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
                  <Tooltip contentStyle={chart.tooltip} />
                  <Bar dataKey="count" radius={[0, 4, 4, 0]}>
                    {typeData.map((entry, i) => (
                      <Cell key={i} fill={typeColour(theme, entry.name)} />
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

      {/* ── Geographic Clusters ─────────────────────────────── */}
      {geographic_clusters && geographic_clusters.length > 0 && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-header">
            <h3>Geographic Hotspots</h3>
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Grouped by 0.1° grid</span>
          </div>
          <div className="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Location</th>
                  <th>Events</th>
                  <th>Dominant Type</th>
                </tr>
              </thead>
              <tbody>
                {geographic_clusters.slice(0, 20).map((c: any, i: number) => (
                  <tr key={i}>
                    <td>#{i + 1}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>
                      {c.lat.toFixed(1)}, {c.lon.toFixed(1)}
                    </td>
                    <td><strong>{c.count}</strong></td>
                    <td>
                      <span
                        className="badge"
                        style={{ background: typeColour(theme, c.dominant_type), color: BADGE_INK[theme] }}
                      >
                        {c.dominant_type.replace(/_/g, " ")}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* ── Recent Events Table ─────────────────────────────── */}
      <div className="card">
        <div className="card-header">
          <h3>Recent Anomaly Events</h3>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {stale && <span style={{ color: "var(--warning)", marginRight: 8 }}>⚠ Stale data</span>}
            {lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : "Auto-refreshes every 10s"}
          </span>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Flagged At</th>
                <th>Hex</th>
                <th>Type</th>
                <th>Lat</th>
                <th>Lon</th>
                <th>Object</th>
              </tr>
            </thead>
            <tbody>
              {[...(recent_events || [])].reverse().map((ev: AnomalyEvent, i: number) => (
                <tr key={`${ev.hex}-${ev.flagged_at ?? i}`}>
                  <td style={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "nowrap" }}>
                    {ev.flagged_at ? formatDateTime(ev.flagged_at) : "—"}
                  </td>
                  <td style={{ fontFamily: "monospace", fontWeight: 600 }}>{ev.hex}</td>
                  <td>
                    <span
                      className="badge"
                      style={{ background: typeColour(theme, ev.reason), color: BADGE_INK[theme] }}
                    >
                      {(ev.reason || "unknown").replace(/_/g, " ")}
                    </span>
                  </td>
                  <td style={{ fontFamily: "monospace", fontSize: 12 }}>{ev.lat?.toFixed(4)}</td>
                  <td style={{ fontFamily: "monospace", fontSize: 12 }}>{ev.lon?.toFixed(4)}</td>
                  <td>{ev.object_type || "—"}</td>
                </tr>
              ))}
              {(!recent_events || recent_events.length === 0) && (
                <tr>
                  <td colSpan={6} style={{ textAlign: "center", padding: 32, color: "var(--text-muted)" }}>
                    No anomaly events recorded yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
