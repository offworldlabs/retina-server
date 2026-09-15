import { api } from "../../api/client";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";

export default function AlertsPage() {
  const { data, loading } = usePolling(
    () => api.alerts().then((d) => (Array.isArray(d) ? d : [])),
    15000,
  );

  if (loading) return <div className="empty-state">Loading…</div>;

  const alerts = data ?? [];
  const severityClass = { info: "online", warning: "warning", error: "offline", critical: "offline" };
  const warnings = alerts.filter((e) => e.severity === "warning");
  const errors = alerts.filter((e) => e.severity === "error" || e.severity === "critical");

  return (
    <>
      <div className="page-header">
        <h1>Alerts & Notifications</h1>
        <p>Stay informed about your nodes and network events</p>
      </div>

      <div className="stats-grid">
        <StatCard label="Total Alerts" value={alerts.length} tone="accent" />
        <StatCard label="Warnings" value={warnings.length} tone="warning" />
        <StatCard label="Errors" value={errors.length} tone="error" />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Recent Alerts</h3>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Auto-refreshes every 15s</span>
        </div>
        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>Time</th>
                <th>Severity</th>
                <th>Category</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {alerts.map((ev, i) => (
                <tr key={i}>
                  <td style={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "nowrap" }}>
                    {ev.ts ? new Date(ev.ts * 1000).toLocaleString() : "—"}
                  </td>
                  <td>
                    <span className={`badge ${severityClass[ev.severity] || "online"}`}>
                      {ev.severity}
                    </span>
                  </td>
                  <td>{ev.category}</td>
                  <td style={{ color: "var(--text-primary)" }}>{ev.message}</td>
                </tr>
              ))}
              {alerts.length === 0 && (
                <tr>
                  <td colSpan={4} style={{ textAlign: "center", padding: 32, color: "var(--text-muted)" }}>
                    No alerts — your nodes are running smoothly!
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
