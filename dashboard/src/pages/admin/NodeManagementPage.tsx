import { useState, useEffect, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { Pager } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import { formatMHz, formatUptime } from "../../utils/format";
import { PositionStatusBadge } from "../../components/PositionStatusBadge";
import { PolledRadars } from "./PolledRadars";
import {
  LocationPrivacyBadge,
  LocationPrivacyControl,
} from "../../components/LocationPrivacyControl";
import { RetnodeLink } from "../../components/RetnodeLink";
import { StatusBadge } from "../../components/StatusBadge";
import { useNodeIds } from "../../hooks/useNodeIds";
import { detectionCount, isOnline } from "../../utils/nodes";
import type { LocationPrivacyState } from "../../types";

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
    // Contacts and owners are each caught on their own so a failure there
    // costs their cells rather than the node list. Logged before the
    // fallback: an empty object is also what "none on file" looks like, and
    // the two should not be indistinguishable in the console.
    const contactsOrNone = api.adminNodeContacts().catch((e) => {
      console.error("contacts unavailable", e);
      return {};
    });
    const ownersOrNone = api.adminNodeOwners().catch((e) => {
      console.error("owners unavailable", e);
      return {};
    });
    const [n, a, c, o] = await Promise.all([api.nodes(), api.analytics(), contactsOrNone, ownersOrNone]);
    const nodeMap = n.nodes || {};
    // Keyed on node_ref: the listing is a public feed and carries no
    // node_id. What needs one joins through useNodeIds below.
    const nodes = Object.entries(nodeMap).map(([ref, info]: [string, any]) => ({
      ...info,
      node_ref: ref,
    }));
    return { nodes, analytics: a, contacts: c, owners: o };
  });
  const { data, loading } = polled;

  const nodes = data?.nodes ?? [];
  const analytics = data?.analytics;
  const contacts = data?.contacts ?? {};
  const owners = data?.owners ?? {};

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
            // its privacy override are all named after it; nothing here may fall
            // back to the ref, which names none of them.
            const nodeId = idsByRef?.[ref] ?? null;
            const summary = summaryMap[ref] || {};
            const contact = nodeId ? contacts[nodeId] : undefined;
            const owner = nodeId ? owners[nodeId] : undefined;
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
                  <span className="meta-label">Uptime</span>
                  <span>{formatUptime(summary.metrics?.uptime_s || 0)}</span>
                  <span className="meta-label">Owner email</span>
                  <span>{owner?.email || "—"}</span>
                  <span className="meta-label">Site contact</span>
                  <span>{contactName(contact)}</span>
                  <span className="meta-label">Site contact email</span>
                  <span>{contact?.email || "—"}</span>
                  <span className="meta-label">Site contact phone</span>
                  <span>{contactPhone(contact)}</span>
                </div>
                <NodeLocationPrivacy nodeId={nodeId} unresolved={idsByRef !== null && !nodeId} />
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

/** Per-node location privacy for the admin list. The admin API answers one
 *  node at a time, so each card asks for its own — which keeps the requests to
 *  the page of cards actually on screen instead of the whole fleet. Clicks are
 *  stopped here: the card around it navigates to the node page.
 *
 *  Addressed by node_id, which is the key the override is stored under. The
 *  route accepts any string, so a ref passed here would be written happily and
 *  then never consulted — hence `unresolved`, which says the id is missing
 *  rather than late and leaves the control unrendered. */
function NodeLocationPrivacy({ nodeId, unresolved }: { nodeId: string | null; unresolved: boolean }) {
  const [state, setState] = useState<LocationPrivacyState | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!nodeId) return;
    let cancelled = false;
    api
      .adminNodeLocationPrivacy(nodeId)
      .then((s) => { if (!cancelled) setState(s); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [nodeId]);

  return (
    <div
      style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border)" }}
      onClick={(e) => e.stopPropagation()}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span className="reading-label">Location privacy</span>
        <LocationPrivacyBadge isPrivate={state?.location_private} />
      </div>
      {unresolved && <div className="privacy-source">No node id for this ref; the override is keyed on one.</div>}
      {!unresolved && failed && <div className="privacy-source">Could not load location privacy.</div>}
      {!unresolved && !failed && !state && <div className="privacy-source">Loading…</div>}
      {state && nodeId && (
        <LocationPrivacyControl
          compact
          nodeId={nodeId}
          isPrivate={state.location_private}
          source={state.location_privacy_source}
          setAt={state.override?.set_at ?? null}
          onSave={(next) => api.setAdminNodeLocationPrivacy(nodeId, next)}
          onReset={() => api.clearAdminNodeLocationPrivacy(nodeId)}
          onApplied={setState}
        />
      )}
    </div>
  );
}
