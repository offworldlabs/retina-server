import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { fmt } from "../../utils/format";

const REFRESH_MS = 5000;

function ago(epoch: number | undefined): string {
  if (!epoch) return "never";
  const s = Math.floor(Date.now() / 1000 - epoch);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function QueueBar({ depth, max, label }: { depth: number; max: number; label: string }) {
  const pct = max > 0 ? Math.min(100, (depth / max) * 100) : 0;
  const color = pct > 80 ? "var(--error)" : pct > 50 ? "var(--warning)" : "var(--success)";
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
        <span>{label}</span>
        <span style={{ color: "var(--text-muted)" }}>
          {depth} / {max} ({fmt(pct, 1)}%)
        </span>
      </div>
      <div style={{ height: 8, background: "var(--bg-secondary)", borderRadius: 4, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${pct}%`, background: color, borderRadius: 4, transition: "width 0.3s" }} />
      </div>
    </div>
  );
}

export default function SystemMetricsPage() {
  const polled = usePolling(() => api.adminMetrics(), REFRESH_MS);
  const { data: m, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const header = (
    <div className="page-header">
      <h1>System Metrics</h1>
      <p>Live operational telemetry — auto-refreshes every 5 s</p>
    </div>
  );
  // Nothing loaded: the first request failed, and the notice says so.
  if (!m) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="system metrics" />
      </>
    );
  }

  const taskNames = Object.keys({ ...m.task_last_success, ...m.task_error_counts });
  const staleSet = new Set<string>(m.stale_tasks ?? []);

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="system metrics" />

      {/* Top stats */}
      <div className="stats-grid">
        <StatCard label="Frames Processed" value={m.frames_processed?.toLocaleString()} />
        <StatCard
          label="Frames Dropped"
          value={m.frames_dropped?.toLocaleString()}
          tone={m.frames_dropped > 0 ? "error" : undefined}
        />
        <StatCard
          label="Active Nodes"
          value={<>{m.connected_nodes} <span style={{ fontSize: 13, color: "var(--text-muted)" }}>/ peak {m.peak_connected_nodes}</span></>}
        />
        <StatCard label="Aircraft on Map" value={m.active_geo_aircraft} />
        <StatCard label="Process RAM" value={<>{fmt(m.process_rss_mb, 0)} MB</>} />
        <StatCard label="Load Avg" value={m.load_avg?.map((v: number) => fmt(v, 2)).join(" / ")} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        {/* Queue utilisation */}
        <div className="card">
          <div className="card-header"><h3>Queue Utilisation</h3></div>
          <div style={{ padding: "0 20px 16px" }}>
            <QueueBar depth={m.frame_queue_depth} max={m.frame_queue_max} label="Frame Queue" />
            <QueueBar depth={m.solver_queue_depth} max={200} label="Solver Queue" />
          </div>
        </div>

        {/* Solver stats */}
        <div className="card">
          <div className="card-header"><h3>Solver</h3></div>
          <div style={{ padding: "0 20px 16px" }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              {[
                ["Successes", m.solver_successes?.toLocaleString()],
                ["Failures", m.solver_failures?.toLocaleString()],
                ["Queue Drops", m.solver_queue_drops?.toLocaleString()],
                ["Last Latency", `${fmt(m.solver_last_latency_s, 3)}s`],
                ["Avg Latency", `${fmt(m.solver_avg_latency_s, 3)}s`],
                ["Queue %", `${fmt(m.solver_queue_pct, 1)}%`],
              ].map(([label, val]) => (
                <div key={label as string}>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 2 }}>{label}</div>
                  <div style={{ fontSize: 16, fontWeight: 600 }}>{val}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        {/* Disk */}
        <div className="card">
          <div className="card-header"><h3>Disk (Archive)</h3></div>
          <div style={{ padding: "0 20px 16px" }}>
            {(() => {
              const total = m.disk_total_gb ?? 1;
              const used = m.disk_used_gb ?? 0;
              const pct = (used / total) * 100;
              const color = pct > 90 ? "var(--error)" : pct > 70 ? "var(--warning)" : "var(--success)";
              return (
                <>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
                    <span>Used: {fmt(used, 1)} GB</span>
                    <span style={{ color: "var(--text-muted)" }}>Free: {fmt(m.disk_free_gb, 1)} GB / {fmt(total, 0)} GB</span>
                  </div>
                  <div style={{ height: 8, background: "var(--bg-secondary)", borderRadius: 4, overflow: "hidden" }}>
                    <div style={{ height: "100%", width: `${pct}%`, background: color, borderRadius: 4 }} />
                  </div>
                  <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 6 }}>{fmt(pct, 1)}% used</div>
                </>
              );
            })()}
          </div>
        </div>

        {/* WebSocket clients */}
        <div className="card">
          <div className="card-header"><h3>WebSocket Clients</h3></div>
          <div style={{ padding: "0 20px 16px", display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            {[
              ["All Clients", m.ws_clients],
              ["Live Clients", m.ws_live_clients],
              ["Multinode Tracks", m.multinode_tracks],
              ["ADS-B Aircraft", m.adsb_aircraft],
            ].map(([label, val]) => (
              <div key={label as string}>
                <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", marginBottom: 2 }}>{label}</div>
                <div style={{ fontSize: 16, fontWeight: 600 }}>{val}</div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Task health */}
      <div className="card">
        <div className="card-header">
          <h3>Background Tasks</h3>
          {staleSet.size > 0 && (
            <span style={{ fontSize: 12, color: "var(--error)", fontWeight: 600 }}>
              {staleSet.size} stale
            </span>
          )}
        </div>
        <DataTable
          headers={["Task", "Status", "Last Success", "Errors"]}
          count={taskNames.length}
          empty="No tasks recorded yet"
        >
          {taskNames.map((name) => {
            const isStale = staleSet.has(name);
            const errors = m.task_error_counts?.[name] ?? 0;
            return (
              <tr key={name}>
                <td style={{ fontFamily: "monospace", fontSize: 13 }}>{name}</td>
                <td>
                  <span className={`badge ${isStale ? "offline" : "online"}`}>
                    {isStale ? "stale" : "ok"}
                  </span>
                </td>
                <td style={{ color: "var(--text-muted)", fontSize: 13 }}>
                  {ago(m.task_last_success?.[name])}
                </td>
                <td>
                  <span style={{ color: errors > 0 ? "var(--error)" : "var(--text-muted)", fontWeight: errors > 0 ? 600 : 400 }}>
                    {errors}
                  </span>
                </td>
              </tr>
            );
          })}
        </DataTable>
      </div>
    </>
  );
}
