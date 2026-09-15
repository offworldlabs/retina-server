import { useState } from "react";
import { api } from "../../api/client";
import { useFetch } from "../../hooks/usePolling";
import { useNodeIds } from "../../components/useNodeIds";

const PAGE_SIZE = 25;

export default function CustodyPage() {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const idsByRef = useNodeIds();
  const { data: custody, loading } = useFetch(() => api.custody());

  if (loading) return <div className="empty-state">Loading…</div>;

  // The custody payload is published, so it is keyed on node_ref throughout.
  const refs = Object.keys(custody?.node_keys || {});
  const filtered = search
    ? refs.filter((ref) =>
        [ref, idsByRef?.[ref]].some((s) => (s || "").toLowerCase().includes(search.toLowerCase())),
      )
    : refs;
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  return (
    <>
      <div className="page-header">
        <h1>Chain of Custody</h1>
        <p>Cryptographic verification and data integrity audit trail</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Registered Nodes</div>
          <div className="stat-value">{custody?.registered_nodes ?? refs.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">With Chain Entries</div>
          <div className="stat-value">
            {refs.filter((ref) => (custody?.chain_entries?.[ref]?.count || 0) > 0).length}
          </div>
        </div>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
        <input
          type="text"
          placeholder="Search nodes…"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(0); }}
          style={{ padding: "6px 12px", borderRadius: 6, border: "1px solid var(--border)", fontSize: 13, width: 260 }}
        />
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
          Showing {paged.length} of {filtered.length} nodes
        </span>
      </div>

      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>Node ref</th>
              <th>Node ID</th>
              <th>Status</th>
              <th>Chain Length</th>
              <th>Latest Hour (UTC)</th>
              <th>IQ Commits</th>
              <th>Signing Mode</th>
              <th>Key Fingerprint</th>
            </tr>
          </thead>
          <tbody>
            {paged.map((ref) => {
              const chain = custody?.chain_entries?.[ref] || {};
              const count = chain.count || 0;
              const verified = chain.latest_verified === true;
              const keyInfo = custody?.node_keys?.[ref] || {};
              return (
                <tr key={ref}>
                  <td style={{ fontFamily: "monospace", fontSize: 12 }}>{ref}</td>
                  <td style={{ fontFamily: "monospace", fontSize: 12, color: "var(--text-muted)" }}>
                    {idsByRef?.[ref] ?? "—"}
                  </td>
                  <td>
                    <span className={`badge ${verified ? "online" : count > 0 ? "warning" : "offline"}`}>
                      {verified ? "Verified" : count > 0 ? "Unverified" : "None"}
                    </span>
                  </td>
                  <td>{count}</td>
                  <td style={{ fontFamily: "monospace", fontSize: 11 }}>{chain.latest_hour || "—"}</td>
                  <td>{custody?.iq_commitments?.[ref] || 0}</td>
                  <td>{keyInfo.signing_mode || "—"}</td>
                  <td style={{ fontFamily: "monospace", fontSize: 11 }}>{keyInfo.fingerprint || "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {totalPages > 1 && (
        <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 12, marginTop: 12 }}>
          <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button>
          <span style={{ fontSize: 12 }}>Page {page + 1} of {totalPages}</span>
          <button className="btn btn-sm" disabled={page >= totalPages - 1} onClick={() => setPage(page + 1)}>Next →</button>
        </div>
      )}

      {filtered.length === 0 && (
        <div className="card">
          <div className="card-body">
            <div className="empty-state">{search ? "No matching nodes" : "No chain of custody data available"}</div>
          </div>
        </div>
      )}
    </>
  );
}
