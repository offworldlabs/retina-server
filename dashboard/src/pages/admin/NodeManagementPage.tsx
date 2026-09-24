import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { Pager } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import { formatAvailability, formatMHz } from "../../utils/format";
import { PositionStatusBadge } from "../../components/PositionStatusBadge";
import { PolledRadars } from "./PolledRadars";
import { RetnodeLink } from "../../components/RetnodeLink";
import { StatusBadge } from "../../components/StatusBadge";
import { useNodeIds } from "../../hooks/useNodeIds";
import { detectionCount, isOnline } from "../../utils/nodes";

const PAGE_SIZE = 25;

// The site contact is whom the node itself names, unverified; the owner is the
// account that claimed it. Every contact field is independently optional
// server side, so each has a row of its own and a dash of its own.
export function contactName(contact) {
  const name = [contact?.first_name, contact?.last_name].filter(Boolean).join(" ");
  return name || "—";
}

// The number as typed, beside the country that makes a national number
// dialable. A country with no number has nothing to resolve.
export function contactPhone(contact) {
  if (!contact?.phone) return "—";
  return contact.country ? `${contact.phone} (${contact.country})` : contact.phone;
}

export default function NodeManagementPage() {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const idsByRef = useNodeIds();
  const navigate = useNavigate();

  const polled = useFetch(async () => {
    // Contacts, owners and reports are each caught on their own so a failure
    // there costs their cells rather than the node list. Logged before the
    // fallback: an empty result is also what "none on file" looks like, and
    // the two should not be indistinguishable in the console.
    const contactsOrNone = api.adminNodeContacts().catch((e) => {
      console.error("contacts unavailable", e);
      return {};
    });
    const ownersOrNone = api.adminNodeOwners().catch((e) => {
      console.error("owners unavailable", e);
      return {};
    });
    // Keyed by node_id inside the chain, so a body it cannot read is caught
    // with the rest.
    const reportsOrNone = api
      .adminNodeReports()
      .then((rows) => Object.fromEntries(rows.map((report) => [report.node_id, report])))
      .catch((e) => {
        console.error("node reports unavailable", e);
        return {};
      });
    const [n, a, c, o, reports] = await Promise.all([
      api.nodes(),
      api.analytics(),
      contactsOrNone,
      ownersOrNone,
      reportsOrNone,
    ]);
    const nodeMap = n.nodes || {};
    // Keyed on node_ref: the listing is a public feed and carries no
    // node_id. What needs one joins through useNodeIds below.
    const nodes = Object.entries(nodeMap).map(([ref, info]: [string, any]) => ({
      ...info,
      node_ref: ref,
    }));
    return { nodes, analytics: a, contacts: c, owners: o, reports };
  });
  const { data, loading } = polled;

  const nodes = data?.nodes ?? [];
  const analytics = data?.analytics;
  const contacts = data?.contacts ?? {};
  const owners = data?.owners ?? {};
  const reports = data?.reports ?? {};

  // Keyed on node_ref, the same key space `nodes` (built above) uses.
  const summaryMap = analytics?.nodes || {};

  // Either identifier finds a node: an operator arrives holding whichever one
  // their last conversation used.
  const matches = (n) =>
    [n.node_ref, idsByRef?.[n.node_ref], n.name]
      .some((s) => (s || "").toLowerCase().includes(search.toLowerCase()));
  const filtered = search ? nodes.filter(matches) : nodes;
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const onlineCount = nodes.filter((n) => isOnline(n.status)).length;

  const header = (
    <div className="page-header">
      <h1>Node Management</h1>
      <p>View and manage all nodes in the network</p>
    </div>
  );
  let nodeList: ReactNode;
  if (loading) {
    nodeList = <div className="empty-state">Loading…</div>;
  } else if (nothingLoaded(polled)) {
    nodeList = <FetchNotice polled={polled} what="the node list" />;
  } else {
    nodeList = (
      <>
        <FetchNotice polled={polled} what="the node list" />

        <div className="stats-grid">
          <StatCard label="Total Nodes" value={nodes.length} tone="accent" />
          <StatCard
            label="Online"
            value={onlineCount}
            tone="success"
          />
          <StatCard
            label="Offline"
            value={nodes.length - onlineCount}
            tone="error"
          />
        </div>

        <div className="toolbar">
          <input
            type="text"
            placeholder="Search nodes…"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(0); }}
            className="input"
          />
          <span className="card-note">
            Showing {paged.length} of {filtered.length} nodes
          </span>
        </div>

        <div className="node-grid">
          {paged.map((node) => {
            const ref = node.node_ref;
            // The private id, or null while the map is in flight and for a ref
            // that resolves to nothing. The node's own site, its contact row and
            // its owner are all named after it; nothing here may fall back to
            // the ref, which names none of them.
            const nodeId = idsByRef?.[ref] ?? null;
            const summary = summaryMap[ref] || {};
            const contact = nodeId ? contacts[nodeId] : undefined;
            const owner = nodeId ? owners[nodeId] : undefined;
            // As of the node's last heartbeat, which names it on contract 1.6.0
            // and later.
            const trackerRelease = nodeId ? reports[nodeId]?.versions?.retina_tracker : undefined;
            return (
              // The node page is addressed by the public identity, since the
              // per-node analytics route behind it is.
              <div className="node-card" key={ref} onClick={() => navigate(`/nodes/${ref}`)}>
                <div className="node-name">
                  <StatusBadge status={node.status} />
                  <PositionStatusBadge status={node.position_status} />
                  <RetnodeLink nodeId={nodeId} synthetic={node.is_synthetic}>
                    {node.name || ref}
                  </RetnodeLink>
                </div>
                <div className="node-meta">
                  <span className="meta-label">Node ref</span>
                  <span className="mono">{ref}</span>
                  <span className="meta-label">Node ID</span>
                  <span className="mono">{nodeId ?? "—"}</span>
                  <span className="meta-label">Frequency</span>
                  <span>{formatMHz(node.frequency)}</span>
                  <span className="meta-label">Detections</span>
                  <span>{detectionCount(summary).toLocaleString()}</span>
                  <span className="meta-label">Frames</span>
                  <span>{(summary.metrics?.total_frames || 0).toLocaleString()}</span>
                  <span className="meta-label">Trust</span>
                  <span>{((summary.trust?.trust_score || 0) * 100).toFixed(0)}%</span>
                  <span className="meta-label">Reputation</span>
                  <span>{((summary.reputation?.reputation || 0) * 100).toFixed(0)}%</span>
                  <span className="meta-label">Avg SNR</span>
                  <span>{(summary.metrics?.avg_snr || 0).toFixed(1)} dB</span>
                  <span className="meta-label">Availability</span>
                  <span>{formatAvailability(summary.metrics?.availability_7d)}</span>
                  <span className="meta-label">Tracker release</span>
                  <span className="mono">{trackerRelease || "—"}</span>
                  <span className="meta-label">Owner email</span>
                  <span>{owner?.email || "—"}</span>
                  <span className="meta-label">Site contact</span>
                  <span>{contactName(contact)}</span>
                  <span className="meta-label">Site contact email</span>
                  <span>{contact?.email || "—"}</span>
                  <span className="meta-label">Site contact phone</span>
                  <span>{contactPhone(contact)}</span>
                </div>
              </div>
            );
          })}
        </div>

        <Pager page={page} totalPages={totalPages} onPage={setPage} />
      </>
    );
  }

  return (
    <>
      {header}
      {nodeList}
      {/* Fetched for itself: a radar on probation is missing from the list above in every state. */}
      <PolledRadars />
    </>
  );
}
