import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { PositionStatusBadge } from "../../components/PositionStatusBadge";
import {
  LocationPrivacyBadge,
  LocationPrivacyControl,
} from "../../components/LocationPrivacyControl";
import type { LocationPrivacyState } from "../../types";

const PAGE_SIZE = 25;

export default function NodeManagementPage() {
  const [nodes, setNodes] = useState([]);
  const [analytics, setAnalytics] = useState(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    Promise.all([api.nodes(), api.analytics()])
      .then(([n, a]) => {
        const nodeMap = n.nodes || {};
        const nodeList = Object.entries(nodeMap).map(([id, info]: [string, any]) => ({ node_id: id, ...info }));
        setNodes(nodeList);
        setAnalytics(a);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const rawSummaries = analytics?.nodes || {};
  const summaries = Array.isArray(rawSummaries) ? rawSummaries : Object.values(rawSummaries);
  const summaryMap = {};
  summaries.forEach((s) => { summaryMap[s.node_id] = s; });

  const filtered = search
    ? nodes.filter((n) => ((n.node_id || n.id || n.name || "")).toLowerCase().includes(search.toLowerCase()))
    : nodes;
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const paged = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  return (
    <>
      <div className="page-header">
        <h1>Node Management</h1>
        <p>View and manage all nodes in the network</p>
      </div>

      <div className="stats-grid">
        <div className="stat-card accent">
          <div className="stat-label">Total Nodes</div>
          <div className="stat-value">{nodes.length}</div>
        </div>
        <div className="stat-card success">
          <div className="stat-label">Online</div>
          <div className="stat-value">
            {nodes.filter((n) => n.status !== "disconnected" && n.status != null).length}
          </div>
        </div>
        <div className="stat-card error">
          <div className="stat-label">Offline</div>
          <div className="stat-value">
            {nodes.filter((n) => n.status === "disconnected" || n.status == null).length}
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

      <div className="node-grid">
        {paged.map((node) => {
          const id = node.node_id || node.id;
          const online = node.status !== "disconnected" && node.status != null;
          const summary = summaryMap[id] || {};
          return (
            <div className="node-card" key={id} onClick={() => navigate(`/nodes/${id}`)}>
              <div className="node-name">
                <span className={`badge ${online ? "online" : "offline"}`}>
                  {online ? "Online" : "Offline"}
                </span>
                <PositionStatusBadge status={node.position_status} />
                {node.name || id}
              </div>
              <div className="node-meta">
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
              </div>
              <NodeLocationPrivacy nodeId={id} />
            </div>
          );
        })}
      </div>

      {totalPages > 1 && (
        <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 12, marginTop: 12 }}>
          <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button>
          <span style={{ fontSize: 12 }}>Page {page + 1} of {totalPages}</span>
          <button className="btn btn-sm" disabled={page >= totalPages - 1} onClick={() => setPage(page + 1)}>Next →</button>
        </div>
      )}
    </>
  );
}

/** Per-node location privacy for the admin list. The admin API answers one
 *  node at a time, so each card asks for its own — which keeps the requests to
 *  the page of cards actually on screen instead of the whole fleet. Clicks are
 *  stopped here: the card around it navigates to the node page. */
function NodeLocationPrivacy({ nodeId }: { nodeId: string }) {
  const [state, setState] = useState<LocationPrivacyState | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
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
      {failed && <div className="privacy-source">Could not load location privacy.</div>}
      {!failed && !state && <div className="privacy-source">Loading…</div>}
      {state && (
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

function formatUptime(seconds) {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return `${h}h ${m}m`;
}
