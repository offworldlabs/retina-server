import { useState } from "react";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager } from "../../components/Pager";
import { useFetch } from "../../hooks/usePolling";
import { LocationPrivacyBadge } from "../../components/LocationPrivacy";
import { RetnodeLink } from "../../components/RetnodeLink";
import { StatusBadge } from "../../components/StatusBadge";
import { isOnline } from "../../utils/nodes";

const PAGE_SIZE = 25;

export default function TunnelLinkPage() {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const polled = useFetch(() => api.myNodes().then((n) => (Array.isArray(n) ? n : [])));
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = data ?? [];

  const header = (
    <div className="page-header">
      <h1>Tunnel & Local Display</h1>
      <p>Access your node&apos;s local radar display remotely</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="your nodes" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="your nodes" />

      <div className="card">
        <div className="card-header"><h3>How It Works</h3></div>
        <div className="card-body">
          <p style={{ color: "var(--text-secondary)", fontSize: 13, lineHeight: 1.8 }}>
            Each Retina node runs a local web display showing real-time radar data.
            When tunnel access is enabled, you can view this display remotely through
            a secure connection, on a hostname of the form <code style={{ background: "var(--bg-input)", padding: "2px 6px", borderRadius: 3 }}>&lt;your-node&gt;.retnode.com</code>.
          </p>
          <p style={{ color: "var(--text-secondary)", fontSize: 13, lineHeight: 1.8, marginTop: 8 }}>
            You can also generate a public shareable link to let others view your node&apos;s display
            (view-only, no control access).
          </p>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <h3>My Nodes</h3>
          <input
            type="text"
            placeholder="Search nodes…"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(0); }}
            className="input input-sm"
          />
        </div>
        {(() => {
          const filtered = search
            ? nodes.filter((n) => `${n.name || ""} ${n.node_ref || ""}`.toLowerCase().includes(search.toLowerCase()))
            : nodes;
          const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
          const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
          return (
            <>
              <DataTable
                headers={["Node", "Status", "Local Display", "Tunnel Status", "Actions"]}
                count={paged.length}
                empty={search ? "No matching nodes" : "No nodes connected yet"}
              >
                {paged.map((node) => {
                  const id = node.node_id;
                  const online = isOnline(node.status);
                  return (
                    <tr key={id}>
                      <td className="mono" style={{ color: "var(--accent)" }}>
                        <RetnodeLink nodeId={id} synthetic={node.is_synthetic}>
                          {node.name || node.node_ref || "—"}
                        </RetnodeLink>{" "}
                        <LocationPrivacyBadge isPrivate={node.location_private} />
                      </td>
                      <td>
                        <StatusBadge status={node.status} />
                      </td>
                      <td className="card-note">
                        {online ? "http://[node-ip]:8080" : "—"}
                      </td>
                      <td>
                        <span className="badge warning">Not Yet Available</span>
                      </td>
                      <td>
                        <button className="btn btn-secondary btn-sm" disabled title="Coming soon">
                          Enable Tunnel
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </DataTable>
              <Pager page={page} totalPages={totalPages} onPage={setPage} note={`${filtered.length} nodes`} />
            </>
          );
        })()}
      </div>

      <div className="card">
        <div className="card-header"><h3>Coming Soon</h3></div>
        <div className="card-body">
          <ul style={{ color: "var(--text-secondary)", fontSize: 13, lineHeight: 2 }}>
            <li>One-click tunnel activation for each node</li>
            <li>Public share link generation (view-only)</li>
            <li>Embedded iframe preview in this dashboard</li>
            <li>Bandwidth usage monitoring per tunnel</li>
          </ul>
        </div>
      </div>
    </>
  );
}
