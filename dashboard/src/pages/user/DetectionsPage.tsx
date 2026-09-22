import { useState } from "react";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager, clampPage } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { useFetch, usePolling } from "../../hooks/usePolling";
import { shortRef } from "../../utils/nodes";

const PAGE_SIZE = 25;

export default function DetectionsPage() {
  const [filterNode, setFilterNode] = useState("");
  const [page, setPage] = useState(0);
  const { data: nodeRefs } = useFetch(() => api.nodes().then((n) => Object.keys(n.nodes || {})));
  const polled = usePolling(
    () => api.aircraft().then((d) => d.aircraft || []),
    3000,
  );
  const { data: feed, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = nodeRefs ?? [];
  const aircraft = feed ?? [];

  // The public aircraft feed renamed node_id to node_ref at publication.
  const filtered = filterNode
    ? aircraft.filter((a) => a.node_ref === filterNode || a.source === filterNode)
    : aircraft;

  const header = (
    <div className="page-header">
      <h1>Live Detections</h1>
      <p>Real-time aircraft feed from the passive radar network</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="detections" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="detections" />

      <div className="stats-grid">
        <StatCard label="Aircraft Tracked" value={filtered.length} tone="accent" />
        <StatCard
          label="With ADS-B Match"
          value={filtered.filter((a) => a.flight || a.hex).length}
          tone="success"
        />
        <StatCard label="Total Network" value={aircraft.length} />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Detection Feed</h3>
          <div className="card-aside">
            <select
              value={filterNode}
              onChange={(e) => { setFilterNode(e.target.value); setPage(0); }}
              style={{
                padding: "4px 8px",
                borderRadius: 6,
                border: "1px solid var(--border)",
                background: "var(--bg-input)",
                color: "var(--text-primary)",
                fontSize: 12,
              }}
            >
              <option value="">All Nodes</option>
              {nodes.map((nid) => (
                <option key={nid} value={nid}>{shortRef(nid)}</option>
              ))}
            </select>
            <span className="card-note">
              Auto-refreshes every 3s
            </span>
          </div>
        </div>
        {(() => {
          const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
          const current = clampPage(page, totalPages);
          const paged = filtered.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
          return (
            <>
              <DataTable
                headers={["Hex", "Flight", "Lat", "Lon", "Alt (ft)", "Speed (kt)", "Track", "Seen (s)"]}
                count={paged.length}
                empty="No detections at this time"
              >
                {paged.map((ac, i) => (
                  <tr key={ac.hex || current * PAGE_SIZE + i}>
                    <td className="mono" style={{ color: "var(--accent)" }}>
                      {ac.hex || "—"}
                    </td>
                    <td style={{ fontWeight: 500, color: "var(--text-primary)" }}>
                      {ac.flight?.trim() || "—"}
                    </td>
                    <td>{ac.lat?.toFixed(4) ?? "—"}</td>
                    <td>{ac.lon?.toFixed(4) ?? "—"}</td>
                    <td>{ac.alt_baro ?? ac.altitude ?? "—"}</td>
                    <td>{ac.gs?.toFixed(0) ?? ac.speed ?? "—"}</td>
                    <td>{ac.track?.toFixed(0) ?? "—"}°</td>
                    <td>{ac.seen?.toFixed(0) ?? "—"}</td>
                  </tr>
                ))}
              </DataTable>
              <Pager page={current} totalPages={totalPages} onPage={setPage} note={`${filtered.length} aircraft`} />
            </>
          );
        })()}
      </div>
    </>
  );
}
