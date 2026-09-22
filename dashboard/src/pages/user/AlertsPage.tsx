import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { severityTone } from "../../utils/severity";

export default function AlertsPage() {
  const polled = usePolling(
    () => api.alerts().then((d) => (Array.isArray(d) ? d : [])),
    15000,
  );
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const alerts = data ?? [];
  const warnings = alerts.filter((e) => e.severity === "warning");
  const errors = alerts.filter((e) => e.severity === "error" || e.severity === "critical");

  const header = (
    <div className="page-header">
      <h1>Alerts & Notifications</h1>
      <p>Stay informed about your nodes and network events</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="alerts" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="alerts" />

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
        <DataTable
          headers={["Time", "Severity", "Category", "Message"]}
          count={alerts.length}
          empty="No alerts — your nodes are running smoothly!"
        >
          {alerts.map((ev, i) => (
            <tr key={i}>
              <td style={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "nowrap" }}>
                {ev.ts ? new Date(ev.ts * 1000).toLocaleString() : "—"}
              </td>
              <td>
                <span className={`badge ${severityTone(ev.severity)}`}>
                  {ev.severity}
                </span>
              </td>
              <td>{ev.category}</td>
              <td style={{ color: "var(--text-primary)" }}>{ev.message}</td>
            </tr>
          ))}
        </DataTable>
      </div>
    </>
  );
}
