import { useState } from "react";
import { api, downloadUrl } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import { formatBytes } from "../../utils/format";

const PAGE_SIZE = 50;

export default function DataExplorerPage() {
  const [page, setPage] = useState(0);
  // Keyed on the page, so turning it fetches again and shows the busy row
  // until the new page lands.
  const { data, pending: loading } = useFetch(() => api.archive(PAGE_SIZE, page * PAGE_SIZE), page);

  const archives = data?.files || [];
  const total = data?.total ?? data?.count ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <>
      <div className="page-header">
        <h1>Data Explorer</h1>
        <p>Browse and download raw detection logs and archive files</p>
      </div>

      <div className="stats-grid">
        <StatCard label="Total Archive Files" value={total.toLocaleString()} tone="accent" />
        <StatCard label="Showing Page" value={<>{page + 1} / {totalPages}</>} />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Archived Detections</h3>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              className="btn btn-secondary"
              disabled={page === 0 || loading}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              ← Prev
            </button>
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total.toLocaleString()}
            </span>
            <button
              className="btn btn-secondary"
              disabled={page >= totalPages - 1 || loading}
              onClick={() => setPage((p) => p + 1)}
            >
              Next →
            </button>
          </div>
        </div>
        <DataTable
          headers={["Filename", "Node", "Size", "Date", "Download"]}
          count={archives.length}
          empty="No archived data available"
          loading={loading}
        >
          {archives.map((file, i) => {
            const key = typeof file === "string" ? file : (file.key || "");
            const parts = key.split("/");
            const name = parts[parts.length - 1] || key;
            // key structure: YYYY/MM/DD/node_id/filename.json
            const node = parts.length >= 4 ? parts[3] : (parts[2] || "—");
            const size = file.size_bytes != null ? formatBytes(file.size_bytes) : "—";
            const date = file.modified ? new Date(file.modified).toLocaleString() : "—";
            return (
              <tr key={i}>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{name}</td>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{node}</td>
                <td>{size}</td>
                <td style={{ fontSize: 12 }}>{date}</td>
                <td>
                  <a
                    href={downloadUrl(`/api/data/archive/${encodeURIComponent(key)}`)}
                    className="btn btn-outline btn-sm"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Download
                  </a>
                </td>
              </tr>
            );
          })}
        </DataTable>
      </div>
    </>
  );
}
