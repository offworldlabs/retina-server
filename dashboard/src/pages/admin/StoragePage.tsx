import { useState, useEffect } from "react";
import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import { formatBytes } from "../../utils/format";

const PAGE_SIZE = 50;

export default function StoragePage() {
  const [page, setPage] = useState(0);
  const { data: storage, refresh: rescan } = useFetch(() => api.adminStorage());
  // If the background scan hasn't completed yet, ask again in 10 s.
  useEffect(() => {
    if (storage?.status !== "initializing") return;
    const timer = setTimeout(rescan, 10000);
    return () => clearTimeout(timer);
  }, [storage, rescan]);
  // Keyed on the page, so turning it fetches again and shows the busy row
  // until the new page lands.
  const { data: archive, pending: loading } = useFetch(() => api.archive(PAGE_SIZE, page * PAGE_SIZE), page);

  const archives = archive?.files || [];
  const total = archive?.total ?? archive?.count ?? 0;

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const perNode: [string, any][] = Object.entries(storage?.per_node ?? {});

  return (
    <>
      <div className="page-header">
        <h1>Data &amp; Storage</h1>
        <p>Storage usage and data management</p>
      </div>

      <div className="stats-grid">
        <StatCard
          label="Archive Files"
          value={storage?.archive_files?.toLocaleString() ?? "—"}
          tone="accent"
        />
        <StatCard
          label="Archive Size"
          value={storage?.archive_mb != null ? storage.archive_mb.toFixed(1) + " MB" : "—"}
        />
        <StatCard
          label="Disk Free"
          value={storage?.disk?.free_gb != null ? storage.disk.free_gb.toFixed(1) + " GB" : "—"}
          tone="success"
        />
        <StatCard
          label="Disk Used"
          value={storage?.disk?.used_pct != null ? storage.disk.used_pct.toFixed(1) + "%" : "—"}
          tone="warning"
        />
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header"><h3>Disk Usage</h3></div>
          <div className="card-body">
            {storage?.disk ? (
              <>
                <div style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
                    <span style={{ color: "var(--text-muted)" }}>
                      {storage.disk.used_gb?.toFixed(1)} GB used of {storage.disk.total_gb?.toFixed(1)} GB
                    </span>
                    <span style={{ fontWeight: 600 }}>{storage.disk.used_pct?.toFixed(1)}%</span>
                  </div>
                  <div style={{
                    height: 8, borderRadius: 4, background: "var(--border)",
                    overflow: "hidden",
                  }}>
                    <div style={{
                      height: "100%", borderRadius: 4,
                      width: `${Math.min(storage.disk.used_pct || 0, 100)}%`,
                      background: (storage.disk.used_pct || 0) > 90 ? "var(--error)"
                        : (storage.disk.used_pct || 0) > 75 ? "var(--warning)" : "var(--success)",
                    }} />
                  </div>
                </div>
                <table>
                  <tbody>
                    <tr>
                      <td style={{ color: "var(--text-muted)" }}>Total</td>
                      <td>{storage.disk.total_gb?.toFixed(2)} GB</td>
                    </tr>
                    <tr>
                      <td style={{ color: "var(--text-muted)" }}>Used</td>
                      <td>{storage.disk.used_gb?.toFixed(2)} GB</td>
                    </tr>
                    <tr>
                      <td style={{ color: "var(--text-muted)" }}>Free</td>
                      <td>{storage.disk.free_gb?.toFixed(2)} GB</td>
                    </tr>
                  </tbody>
                </table>
              </>
            ) : (
              <p style={{ color: "var(--text-muted)" }}>
                {storage?.status === "initializing" ? "Scan in progress…" : "No disk data"}
              </p>
            )}
          </div>
        </div>

        <div className="card">
          <div className="card-header"><h3>Write Rate</h3></div>
          <div className="card-body">
            {storage?.write_rate ? (
              <table>
                <tbody>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Total Write Rate</td>
                    <td>{storage.write_rate.total_mb_per_day?.toFixed(2)} MB/day</td>
                  </tr>
                  <tr>
                    <td style={{ color: "var(--text-muted)" }}>Est. Days Until Full</td>
                    <td style={{
                      fontWeight: 600,
                      color: (storage.write_rate.days_until_full || 0) < 30 ? "var(--error)"
                        : (storage.write_rate.days_until_full || 0) < 90 ? "var(--warning)" : "var(--success)",
                    }}>
                      {storage.write_rate.days_until_full > 0
                        ? storage.write_rate.days_until_full > 365
                          ? `${(storage.write_rate.days_until_full / 365).toFixed(1)} years`
                          : `${storage.write_rate.days_until_full} days`
                        : "—"}
                    </td>
                  </tr>
                </tbody>
              </table>
            ) : (
              <p style={{ color: "var(--text-muted)" }}>
                {storage?.status === "initializing" ? "Scan in progress…" : "No write rate data"}
              </p>
            )}
          </div>
        </div>
      </div>

      <div className="grid-2" style={{ marginTop: 16 }}>
        <div className="card">
          <div className="card-header"><h3>Local Storage</h3></div>
          <div className="card-body">
            <table>
              <tbody>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Archive Files</td>
                  <td>{storage?.archive_files?.toLocaleString() ?? "—"}</td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Total Size</td>
                  <td>{storage?.archive_mb?.toFixed(2) ?? "0"} MB</td>
                </tr>
                <tr>
                  <td style={{ color: "var(--text-muted)" }}>Raw Bytes</td>
                  <td>{(storage?.archive_bytes || 0).toLocaleString()}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

      </div>

      {perNode.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-header"><h3>Storage by Node</h3></div>
          <DataTable
            headers={["Node", "Files", "Size", "Write Rate"]}
            count={perNode.length}
          >
            {perNode.map(([nodeId, info]) => {
              const rate = storage.write_rate?.per_node_bytes_per_day?.[nodeId] || 0;
              return (
                <tr key={nodeId}>
                  <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--accent)" }}>{nodeId}</td>
                  <td>{(info.files || 0).toLocaleString()}</td>
                  <td>{formatBytes(info.bytes || 0)}</td>
                  <td>{rate > 0 ? formatBytes(rate) + "/day" : "—"}</td>
                </tr>
              );
            })}
          </DataTable>
        </div>
      )}

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-header">
          <h3>Recent Archives</h3>
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
          headers={["Filename", "Node", "Size", "Date"]}
          count={archives.length}
          empty="No archives"
          loading={loading}
        >
          {archives.map((file, i) => {
            const key = typeof file === "string" ? file : (file.key || "");
            const parts = key.split("/");
            const name = parts[parts.length - 1] || key;
            const node = parts.length >= 4 ? parts[3] : "—";
            const size = file.size_bytes != null ? formatBytes(file.size_bytes) : "—";
            const date = file.modified ? new Date(file.modified).toLocaleString() : "—";
            return (
              <tr key={i}>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{name}</td>
                <td style={{ fontFamily: "monospace", fontSize: 12 }}>{node}</td>
                <td>{size}</td>
                <td style={{ fontSize: 12 }}>{date}</td>
              </tr>
            );
          })}
        </DataTable>
      </div>
    </>
  );
}
