import { useState } from "react";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { severityTone } from "../../utils/severity";

const PAGE_SIZE = 25;
const REFRESH_MS = 15_000;

const ALERT_SEVERITIES = new Set(["warning", "error", "critical"]);
const ALERT_CATEGORIES = new Set(["node", "config", "system"]);

type LogEvent = { ts?: number; severity?: string; category?: string; message?: string };

/** Anything worse than info, and every event about a node, its configuration or the server. */
function isAlert(ev: LogEvent): boolean {
  return ALERT_SEVERITIES.has(ev.severity ?? "") || ALERT_CATEGORIES.has(ev.category ?? "");
}

export default function EventsPage() {
  const [page, setPage] = useState(0);
  const [alertsOnly, setAlertsOnly] = useState(false);
  // New events arrive at the top, so a refresh would slide every older page
  // under its reader. Only the first page follows the log; the others read
  // the log as it stood when the reader left it.
  const [held, setHeld] = useState<LogEvent[] | null>(null);
  const polled = usePolling(
    () => api.adminEvents(500).then((d): LogEvent[] => (Array.isArray(d) ? d : [])),
    page === 0 ? REFRESH_MS : 0,
  );
  const { data, loading } = polled;

  const turnTo = (next: number) => {
    setHeld(next === 0 ? null : (held ?? data));
    setPage(next);
  };

  if (loading) return <div className="empty-state">Loading…</div>;

  const log = held ?? data ?? [];
  const events = alertsOnly ? log.filter(isAlert) : log;
  const totalPages = Math.ceil(events.length / PAGE_SIZE);
  const paged = events.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  const header = (
    <div className="page-header">
      <h1>Events & Alerts</h1>
      <p>Structured event log from the network</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="events" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="events" />

      <div className="stats-grid">
        <StatCard label={alertsOnly ? "Alerts" : "Total Events"} value={events.length} />
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
          <div className="card-aside">
            <span className="card-note">
              Showing {paged.length} of {events.length} ·{" "}
              {page === 0 ? `refreshes every ${REFRESH_MS / 1000}s` : "refresh paused"}
            </span>
            <button
              className="btn btn-secondary btn-sm"
              aria-pressed={alertsOnly}
              onClick={() => {
                setAlertsOnly((on) => !on);
                turnTo(0);
              }}
            >
              Alerts only
            </button>
          </div>
        </div>
        <DataTable
          headers={["Time", "Severity", "Category", "Message"]}
          count={paged.length}
          empty={alertsOnly ? "No alerts in the log" : "No events recorded yet"}
        >
          {paged.map((ev, i) => (
            <tr key={page * PAGE_SIZE + i}>
              <td className="mono" style={{ whiteSpace: "nowrap" }}>
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
        <Pager page={page} totalPages={totalPages} onPage={turnTo} />
      </div>
    </>
  );
}
