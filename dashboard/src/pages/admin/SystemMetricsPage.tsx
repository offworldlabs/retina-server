import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { UsageBar } from "../../components/UsageBar";
import { usePolling } from "../../hooks/usePolling";
import { fmt, formatRelativeTime } from "../../utils/format";

const REFRESH_MS = 5000;

function QueueBar({ depth, max, label }: { depth: number; max: number; label: string }) {
  const pct = max > 0 ? Math.min(100, (depth / max) * 100) : 0;
  return <UsageBar label={label} value={<>{depth} / {max} ({fmt(pct, 1)}%)</>} pct={pct} />;
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
          value={m.connected_nodes}
          unit={`/ peak ${m.peak_connected_nodes}`}
        />
        <StatCard label="Aircraft on Map" value={m.active_geo_aircraft} />
        <StatCard label="Process RAM" value={<>{fmt(m.process_rss_mb, 0)} MB</>} />
        <StatCard label="Load Avg" value={m.load_avg?.map((v: number) => fmt(v, 2)).join(" / ")} />
      </div>

      <div className="grid-2">
        {/* Queue utilisation */}
        <div className="card">
          <div className="card-header"><h3>Queue Utilisation</h3></div>
          <div className="card-body">
            <QueueBar depth={m.frame_queue_depth} max={m.frame_queue_max} label="Frame Queue" />
            <QueueBar depth={m.solver_queue_depth} max={200} label="Solver Queue" />
          </div>
        </div>

        {/* Solver stats */}
        <div className="card">
          <div className="card-header"><h3>Solver</h3></div>
          <div className="card-body">
            <div className="readings">
              {[
                ["Successes", m.solver_successes?.toLocaleString()],
                ["Failures", m.solver_failures?.toLocaleString()],
                ["Queue Drops", m.solver_queue_drops?.toLocaleString()],
                ["Last Latency", `${fmt(m.solver_last_latency_s, 3)}s`],
                ["Avg Latency", `${fmt(m.solver_avg_latency_s, 3)}s`],
                ["Queue %", `${fmt(m.solver_queue_pct, 1)}%`],
              ].map(([label, val]) => (
                <div key={label as string}>
                  <div className="reading-label">{label}</div>
                  <div className="reading-value">{val}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      <div className="grid-2">
        {/* Disk */}
        <div className="card">
          <div className="card-header"><h3>Disk (Archive)</h3></div>
          <div className="card-body">
            {(() => {
              const total = m.disk_total_gb ?? 1;
              const used = m.disk_used_gb ?? 0;
              const pct = (used / total) * 100;
              return (
                <UsageBar
                  label={<>Used: {fmt(used, 1)} GB</>}
                  value={<>Free: {fmt(m.disk_free_gb, 1)} GB / {fmt(total, 0)} GB</>}
                  pct={pct}
                  note={<>{fmt(pct, 1)}% used</>}
                />
              );
            })()}
          </div>
        </div>

        {/* WebSocket clients */}
        <div className="card">
          <div className="card-header"><h3>WebSocket Clients</h3></div>
          <div className="card-body readings">
            {[
              ["All Clients", m.ws_clients],
              ["Live Clients", m.ws_live_clients],
              ["Multinode Tracks", m.multinode_tracks],
              ["ADS-B Aircraft", m.adsb_aircraft],
            ].map(([label, val]) => (
              <div key={label as string}>
                <div className="reading-label">{label}</div>
                <div className="reading-value">{val}</div>
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
                <td className="mono">{name}</td>
                <td>
                  <span className={`badge ${isStale ? "offline" : "online"}`}>
                    {isStale ? "stale" : "ok"}
                  </span>
                </td>
                <td className="muted">
                  {formatRelativeTime(m.task_last_success?.[name])}
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
