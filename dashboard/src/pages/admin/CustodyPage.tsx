import { useState } from "react";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import { useNodeIds } from "../../hooks/useNodeIds";

const PAGE_SIZE = 25;

export default function CustodyPage() {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const idsByRef = useNodeIds();
  const polled = useFetch(() => api.custody());
  const { data: custody, loading } = polled;

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

  const header = (
    <div className="page-header">
      <h1>Chain of Custody</h1>
      <p>Cryptographic verification and data integrity audit trail</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="custody records" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="custody records" />

      <div className="stats-grid">
        <StatCard label="Registered Nodes" value={custody?.registered_nodes ?? refs.length} tone="accent" />
        <StatCard
          label="With Chain Entries"
          value={refs.filter((ref) => (custody?.chain_entries?.[ref]?.count || 0) > 0).length}
          tone="success"
        />
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

      <DataTable
        headers={["Node ref", "Node ID", "Status", "Chain Length", "Latest Hour (UTC)", "IQ Commits", "Signing Mode", "Key Fingerprint"]}
        count={paged.length}
        empty={search ? "No matching nodes" : "No chain of custody data available"}
      >
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
      </DataTable>

      <Pager page={page} totalPages={totalPages} onPage={setPage} />
    </>
  );
}
