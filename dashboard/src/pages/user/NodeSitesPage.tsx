import { useState } from "react";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager, clampPage } from "../../components/Pager";
import { useFetch } from "../../hooks/usePolling";
import { LocationPrivacyBadge } from "../../components/LocationPrivacy";
import { RetnodeLink } from "../../components/RetnodeLink";
import { StatusBadge } from "../../components/StatusBadge";

const PAGE_SIZE = 25;

export default function NodeSitesPage() {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const polled = useFetch(() => api.myNodes().then((n) => (Array.isArray(n) ? n : [])));
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = data ?? [];

  const header = (
    <div className="page-header">
      <h1>Node Sites</h1>
      <p>
        Each Retina node serves its own site at <code className="mono">&lt;node id&gt;.retnode.com</code>.
        Open it from here; a stock blah2 radar has none.
      </p>
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
          const current = clampPage(page, totalPages);
          const paged = filtered.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
          return (
            <>
              <DataTable
                headers={["Node", "Status"]}
                count={paged.length}
                empty={search ? "No matching nodes" : "No nodes connected yet"}
              >
                {paged.map((node) => {
                  const id = node.node_id;
                  return (
                    <tr key={id}>
                      <td className="mono" style={{ color: "var(--accent)" }}>
                        {/* The server polls a stock radar; it serves no site of its own. */}
                        <RetnodeLink nodeId={node.polled ? null : id} synthetic={node.is_synthetic}>
                          {node.name || node.node_ref || "—"}
                        </RetnodeLink>{" "}
                        <LocationPrivacyBadge isPrivate={node.location_private} />
                      </td>
                      <td>
                        <StatusBadge status={node.status} />
                      </td>
                    </tr>
                  );
                })}
              </DataTable>
              <Pager page={current} totalPages={totalPages} onPage={setPage} note={`${filtered.length} nodes`} />
            </>
          );
        })()}
      </div>
    </>
  );
}
