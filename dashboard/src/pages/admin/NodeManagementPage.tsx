import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { Pager } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import { formatUptime } from "../../utils/format";
import { PositionStatusBadge } from "../../components/PositionStatusBadge";
import {
  LocationPrivacyBadge,
  LocationPrivacyControl,
} from "../../components/LocationPrivacyControl";
import { RetnodeLink } from "../../components/RetnodeLink";
import { useNodeIds } from "../../components/useNodeIds";
import type { LocationPrivacyState } from "../../types";

const PAGE_SIZE = 25;

// Both identifiers are opaque strings read character by character when they are
// compared against something else on screen.
const MONO = { fontFamily: "monospace", fontSize: 12 } as const;

// Every field is independently optional server side, so a contact can be a
// phone number and nothing else; falling through to it is what keeps such a
// node from reading as "nobody reported anything".
export function contactLabel(contact) {
  if (!contact) return "—";
  const name = [contact.first_name, contact.last_name].filter(Boolean).join(" ");
  return name || contact.email || contact.phone || "—";
}

export default function NodeManagementPage() {
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const idsByRef = useNodeIds();
  const navigate = useNavigate();

  const { data, loading } = useFetch(async () => {
    // Contacts are caught on their own so a failure there costs the contact
    // cells rather than the node list. Logged before the fallback: an empty
    // object is also what "nobody has reported one" looks like, and the two
    // should not be indistinguishable in the console.
    const contactsOrNone = api.adminNodeContacts().catch((e) => {
      console.error("contacts unavailable", e);
      return {};
    });
    const [n, a, c] = await Promise.all([api.nodes(), api.analytics(), contactsOrNone]);
    const nodeMap = n.nodes || {};
    // Keyed on node_ref: the listing is a public feed and carries no
    // node_id. What needs one joins through useNodeIds below.
    const nodes = Object.entries(nodeMap).map(([ref, info]: [string, any]) => ({
      ...info,
      node_ref: ref,
    }));
    return { nodes, analytics: a, contacts: c };
  });

  if (loading) return <div className="empty-state">Loading…</div>;

  const nodes = data?.nodes ?? [];
  const analytics = data?.analytics;
  const contacts = data?.contacts ?? {};

  const rawSummaries = analytics?.nodes || {};
  // Keyed on node_ref, the same key space `nodes` (built above) uses; summary
  // values no longer carry node_id to key off instead.
  const summaryMap = {};
  if (Array.isArray(rawSummaries)) {
    rawSummaries.forEach((s) => { summaryMap[s.node_ref] = s; });
  } else {
    Object.entries(rawSummaries).forEach(([ref, s]) => { summaryMap[ref] = s; });
  }

  // Either identifier finds a node: an operator arrives holding whichever one
  // their last conversation used.
  const matches = (n) =>
    [n.node_ref, idsByRef?.[n.node_ref], n.name]
      .some((s) => (s || "").toLowerCase().includes(search.toLowerCase()));
  const filtered = search ? nodes.filter(matches) : nodes;
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  return (
    <>
      <div className="page-header">
        <h1>Node Management</h1>
        <p>View and manage all nodes in the network</p>
      </div>

      <div className="stats-grid">
        <StatCard label="Total Nodes" value={nodes.length} tone="accent" />
        <StatCard
          label="Online"
          value={nodes.filter((n) => n.status !== "disconnected" && n.status != null).length}
          tone="success"
        />
        <StatCard
          label="Offline"
          value={nodes.filter((n) => n.status === "disconnected" || n.status == null).length}
          tone="error"
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

      <div className="node-grid">
        {paged.map((node) => {
          const ref = node.node_ref;
          // The private id, or null while the map is in flight and for a ref
          // that resolves to nothing. The node's own site, its contact row and
          // its privacy override are all named after it; nothing here may fall
          // back to the ref, which names none of them.
          const nodeId = idsByRef?.[ref] ?? null;
          const online = node.status !== "disconnected" && node.status != null;
          const summary = summaryMap[ref] || {};
          const contact = nodeId ? contacts[nodeId] : undefined;
          const contactText = contactLabel(contact);
          // The label is the name when there is one, so the address is worth a
          // tooltip only then; otherwise it is already what the cell shows.
          const named = Boolean(contact?.first_name || contact?.last_name);
          const contactTitle = named ? contact.email || undefined : undefined;
          return (
            // The node page is addressed by the public identity, since the
            // per-node analytics route behind it is.
            <div className="node-card" key={ref} onClick={() => navigate(`/nodes/${ref}`)}>
              <div className="node-name">
                <span className={`badge ${online ? "online" : "offline"}`}>
                  {online ? "Online" : "Offline"}
                </span>
                <PositionStatusBadge status={node.position_status} />
                <RetnodeLink nodeId={nodeId} synthetic={node.is_synthetic}>
                  {node.name || ref}
                </RetnodeLink>
              </div>
              <div className="node-meta">
                <span className="meta-label">Node ref</span>
                <span style={MONO}>{ref}</span>
                <span className="meta-label">Node ID</span>
                <span style={MONO}>{nodeId ?? "—"}</span>
                <span className="meta-label">Frequency</span>
                <span>{node.frequency ? `${(node.frequency / 1e6).toFixed(2)} MHz` : "—"}</span>
                <span className="meta-label">Detections</span>
                <span>{(summary.metrics?.total_detections || summary.detection_area?.n_detections || 0).toLocaleString()}</span>
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
                <span className="meta-label">Contact</span>
                <span title={contactTitle}>{contactText}</span>
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
        <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.05em", color: "var(--text-muted)" }}>
          Location privacy
        </span>
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
