import { useState } from "react";
import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { Pager } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";

const PAGE_SIZE = 25;

export default function EventsPage() {
  const [page, setPage] = useState(0);
  const { data, loading } = useFetch(() =>
    api.adminEvents(500).then((d) => (Array.isArray(d) ? d : [])),
  );

  if (loading) return <div className="empty-state">Loading…</div>;

  const events = data ?? [];
  const severityClass = { info: "online", warning: "warning", error: "offline", critical: "offline" };
  const totalPages = Math.ceil(events.length / PAGE_SIZE);
  const paged = events.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  return (
    <>
      <div className="page-header">
        <h1>Events & Alerts</h1>
        <p>Structured event log from the network</p>
      </div>

      <div className="stats-grid">
        <StatCard label="Total Events" value={events.length} />
        <StatCard
          label="Warnings"
          value={events.filter((e) => e.severity === "warning").length}
          tone="warning"
        />
        <StatCard
          label="Errors"
          value={events.filter((e) => e.severity === "error" || e.severity === "critical").length}
          tone="error"
        />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Event Log</h3>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Showing {paged.length} of {events.length} events
          </span>
        </div>
        <DataTable
          headers={["Time", "Severity", "Category", "Message"]}
          count={paged.length}
          empty="No events recorded yet"
        >
          {paged.map((ev, i) => (
            <tr key={page * PAGE_SIZE + i}>
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
        </DataTable>
        <Pager page={page} totalPages={totalPages} onPage={setPage} />
      </div>
    </>
  );
}
