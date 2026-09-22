import { useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { api } from "../../api/client";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import { formatUptime } from "../../utils/format";
import { useChartTheme } from "../../utils/chartTheme";
import { RetnodeLink } from "../../components/RetnodeLink";
import { POSITION_STATUS_EXPLANATION } from "../../components/PositionStatusBadge";
import {
  LocationPrivacyBadge,
  LocationPrivacyControl,
} from "../../components/LocationPrivacyControl";
import type { LocationPrivacyState, PositionStatus } from "../../types";

const POSITION_FIX_HINT: Record<Exclude<PositionStatus, "positioned">, string> = {
  missing_rx: "Add its receiver position in the node configuration.",
  missing_tx: "Add its illuminator position in the node configuration.",
  missing_both: "Add its receiver and illuminator positions in the node configuration.",
};

export default function NodeDetailPage() {
  const { nodeId } = useParams();
  // A route change is a new identity, including pending reads, optimistic
  // privacy controls and saves still completing for the previous node.
  return <NodeDetail key={nodeId} nodeId={nodeId} />;
}

function NodeDetail({ nodeId }: { nodeId: string | undefined }) {
  const chart = useChartTheme();
  const navigate = useNavigate();

  // Keyed on the route parameter, so moving between nodes fetches again.
  const { data: page, loading, error } = useFetch(async () => {
    const [analytics, nodeData, mine] = await Promise.all([
      api.nodeAnalytics(nodeId),
      // Both of these fail soft: a private node is absent from /api/radar/nodes
      // and myNodes 401s for a signed-out viewer, but the per-node analytics
      // route answers its owner, so the page must render on that alone.
      api.nodes().catch(() => ({ nodes: {} })),
      api.myNodes().catch(() => []),
    ]);
    // The route parameter is a public identity — a node_ref, or a synthetic
    // node's own id, which is what it publishes as. The owner's list is the
    // one place both identifiers appear together, so it is matched on the
    // ref first; node_id covers the synthetic case, where the two are equal.
    const owned = (Array.isArray(mine) ? mine : []).find(
      (n) => (n.node_ref && n.node_ref === nodeId) || n.node_id === nodeId,
    );
    return {
      analytics,
      nodeInfo: (nodeData?.nodes || {})[nodeId] || null,
      // Ownership, not presence in the node list, is what earns the privacy
      // card — the node the card matters most for is the one missing there.
      privacy: owned
        ? {
            node_id: owned.node_id,
            location_private: !!owned.location_private,
            location_privacy_source: owned.location_privacy_source || "default",
          }
        : null,
      // Same gate as the privacy card, and for the same reason: this is the
      // owner's own view of their own node. Null for a node nobody claimed by
      // email, which is every node an administrator assigned.
      ownership: owned ? { node_id: owned.node_id, claimed_with: owned.claimed_with || null } : null,
    };
  }, nodeId ?? "");
  // What the privacy control last saved. It outranks the fetched answer for
  // the node it was saved on, and lapses when the route moves to another.
  const [applied, setApplied] = useState<{ nodeId: string; privacy: LocationPrivacyState } | null>(null);
  // Every hook stays above the early returns below: a render that takes the
  // loading or not-found path must call exactly as many as one that does not.
  //
  // `released` is the node handed back in this session. It outranks the fetched
  // answer for that node, because the ownership and privacy cards both gate on
  // ownership and leaving them up would offer controls the server now answers
  // 404 for.
  const [released, setReleased] = useState<string | null>(null);
  const [confirmingRelease, setConfirmingRelease] = useState(false);
  const [releasing, setReleasing] = useState(false);
  const [releaseError, setReleaseError] = useState<string | null>(null);

  if (loading) return <div className="empty-state">Loading…</div>;
  // A failed fetch is this node not being found, not the previous node's
  // details standing in for it.
  const data = error ? null : page?.analytics;
  if (!data) return <div className="empty-state">Node not found</div>;

  const nodeInfo = page?.nodeInfo ?? null;
  const privacy: LocationPrivacyState | null =
    released === nodeId ? null : applied?.nodeId === nodeId ? applied.privacy : (page?.privacy ?? null);
  const ownership = released === nodeId ? null : (page?.ownership ?? null);

  async function release() {
    if (!ownership) return;
    setReleasing(true);
    setReleaseError(null);
    try {
      await api.releaseNode(ownership.node_id);
      setReleased(nodeId);
      setConfirmingRelease(false);
    } catch (e) {
      setReleaseError((e as Error).message || "Could not release this node.");
    } finally {
      setReleasing(false);
    }
  }
  // What this node publishes as, and what the URL addresses it by: the public
  // analytics payload is keyed on the ref and carries no node_id of its own.
  const nodeRef = data.node_ref || nodeId;
  // A node's own site is named after its private node_id, so the link can only
  // be offered to someone who already holds that id — its owner, via the
  // owner-scoped node list. For everyone else RetnodeLink has nothing to open
  // and renders the label as plain text, which is the whole point of the ref.
  const ownId = privacy?.node_id || "";
  const metrics = data.metrics || data;
  const trust = data.trust || {};
  const reputation = data.reputation || {};
  const gapStats = metrics.gap_stats || {};

  // Build SNR-like chart from available data
  const barData = [
    { name: "Avg SNR", value: metrics.avg_snr || 0 },
    { name: "Trust", value: (trust.trust_score || 0) * 100 },
    { name: "Reputation", value: (reputation.reputation || 0) * 100 },
  ];

  return (
    <>
      <div className="page-header">
        <h1 style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="btn btn-outline btn-sm" onClick={() => navigate(-1)}>← Back</button>
          <RetnodeLink nodeId={ownId} synthetic={nodeInfo?.is_synthetic}>
            {nodeRef}
          </RetnodeLink>
        </h1>
        <p>Detailed metrics and trust analysis</p>
      </div>

      <div className="stats-grid">
        <StatCard label="Total Frames" value={(metrics.total_frames || 0).toLocaleString()} tone="accent" />
        <StatCard
          label="Total Detections"
          value={(metrics.total_detections || 0).toLocaleString()}
          tone="success"
        />
        <StatCard label="Total Tracks" value={metrics.total_tracks || 0} />
        <StatCard label="Avg SNR" value={<>{(metrics.avg_snr || 0).toFixed(1)} dB</>} />
      </div>

      <div className="grid-2">
        {/* Trust & Reputation */}
        <div className="card">
          <div className="card-header"><h3>Trust & Reputation</h3></div>
          <div className="card-body">
            <div className="chart-container" style={{ height: 200 }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={barData}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis dataKey="name" stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} />
                  <Tooltip
                    contentStyle={chart.tooltip}
                  />
                  <Bar dataKey="value" fill={chart.series[0]} radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <table>
              <tbody>
                <tr><td>Trust Score</td><td>{((trust.trust_score || 0) * 100).toFixed(1)}%</td></tr>
                <tr><td>ADS-B Matches</td><td>{trust.adsb_matches || 0}</td></tr>
                <tr><td>ADS-B Misses</td><td>{trust.adsb_misses || 0}</td></tr>
                <tr><td>Reputation</td><td>{((reputation.reputation || 0) * 100).toFixed(1)}%</td></tr>
                <tr><td>Penalties</td><td>{reputation.n_penalties || 0}</td></tr>
                <tr><td>Blocked</td><td>{reputation.blocked ? "Yes" : "No"}</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        {/* Gap / Timing Stats */}
        <div className="card">
          <div className="card-header"><h3>Timing & Gaps</h3></div>
          <div className="card-body">
            <table>
              <tbody>
                <tr><td>Uptime</td><td>{formatUptime(metrics.uptime_s || 0)}</td></tr>
                <tr><td>Average Gap</td><td>{(gapStats.avg_gap || 0).toFixed(2)}s</td></tr>
                <tr><td>Max Gap</td><td>{(gapStats.max_gap || 0).toFixed(2)}s</td></tr>
                <tr><td>Gap Std Dev</td><td>{(gapStats.std_gap || 0).toFixed(3)}s</td></tr>
                <tr><td>Total Gaps</td><td>{gapStats.n_gaps || 0}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Location privacy — owners only; the control writes the /me routes. */}
      {privacy && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header">
            <h3>Location privacy</h3>
            <LocationPrivacyBadge isPrivate={privacy.location_private} />
          </div>
          <div className="card-body">
            <LocationPrivacyControl
              nodeId={privacy.node_id}
              isPrivate={privacy.location_private}
              source={privacy.location_privacy_source}
              uncertaintyKm={data.detection_area?.rx?.location_uncertainty_km}
              onSave={(next) => api.myNodeLocationPrivacy(privacy.node_id, next)}
              onReset={() => api.clearMyNodeLocationPrivacy(privacy.node_id)}
              onApplied={(next) => setApplied({ nodeId, privacy: next })}
            />
          </div>
        </div>
      )}

      {/* Ownership — owners only. Releasing is here rather than on a list page
          because it wants the node named in front of it. */}
      {ownership && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header"><h3>Ownership</h3></div>
          <div className="card-body stack">
            <p>
              {ownership.claimed_with
                ? <>Claimed with <strong>{ownership.claimed_with}</strong>.</>
                : <>This node was assigned to you rather than claimed with an email address.</>}
            </p>
            <p className="muted">
              Releasing hands it back so its next owner can claim it. You stop seeing its unpublished
              data straight away, and the node learns within a minute.
            </p>
            {releaseError && <p className="login-error">{releaseError}</p>}
            {confirmingRelease ? (
              <>
                <p><strong>Release this node?</strong> Whoever claims it next becomes its owner.</p>
                <div className="btn-row">
                  <button className="btn btn-danger" onClick={release} disabled={releasing}>
                    {releasing ? "Releasing…" : "Yes, release it"}
                  </button>
                  <button className="btn btn-outline" onClick={() => setConfirmingRelease(false)} disabled={releasing}>
                    Keep it
                  </button>
                </div>
              </>
            ) : (
              <button className="btn btn-outline" onClick={() => setConfirmingRelease(true)}>
                Release this node
              </button>
            )}
          </div>
        </div>
      )}

      {/* Detection Area */}
      {data.detection_area && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header"><h3>Detection Area</h3></div>
          <div className="card-body">
            <table>
              <tbody>
                <tr><td>Estimated Range</td><td>{(data.detection_area.estimated_range_km || 0).toFixed(1)} km</td></tr>
                <tr><td>Beam Width</td><td>{(data.detection_area.beam_width_deg || 0).toFixed(1)}°</td></tr>
                <tr><td>ADS-B Validated Positions</td><td>{data.detection_area.validated_positions || 0}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Position completeness is orthogonal to the node's liveness (`status`):
          a positionless node can be actively detecting and perfectly healthy. */}
      {nodeInfo?.position_status && nodeInfo.position_status !== "positioned" && (
        <div
          style={{
            marginBottom: 24,
            padding: "12px 16px",
            borderRadius: 8,
            border: "1px solid var(--warning)",
            background: "var(--warning-light)",
            fontSize: 13,
          }}
        >
          <strong>Position not configured.</strong>{" "}
          {POSITION_STATUS_EXPLANATION}{" "}
          {POSITION_FIX_HINT[nodeInfo.position_status as Exclude<PositionStatus, "positioned">]}
        </div>
      )}

      {/* RF Configuration */}
      {nodeInfo && (
        <div className="card" style={{ marginBottom: 24 }}>
          <div className="card-header"><h3>RF Configuration</h3></div>
          <div className="card-body">
            <table>
              <tbody>
                <tr>
                  <td>Center Frequency</td>
                  <td>{nodeInfo.frequency ? `${(nodeInfo.frequency / 1e6).toFixed(3)} MHz` : "—"}</td>
                </tr>
                <tr>
                  <td>Sample Rate</td>
                  <td>{nodeInfo.sample_rate ? `${(nodeInfo.sample_rate / 1e6).toFixed(2)} MSps` : "—"}</td>
                </tr>
                <tr>
                  <td>RX Location</td>
                  <td>
                    {nodeInfo.location?.rx_lat != null
                      ? `${nodeInfo.location.rx_lat.toFixed(5)}, ${nodeInfo.location.rx_lon.toFixed(5)}`
                      : "—"}
                    {nodeInfo.location?.rx_alt_ft != null ? ` / ${nodeInfo.location.rx_alt_ft} ft` : ""}
                  </td>
                </tr>
                <tr>
                  <td>TX Location</td>
                  <td>
                    {nodeInfo.location?.tx_lat != null
                      ? `${nodeInfo.location.tx_lat.toFixed(5)}, ${nodeInfo.location.tx_lon.toFixed(5)}`
                      : "—"}
                    {nodeInfo.location?.tx_alt_ft != null ? ` / ${nodeInfo.location.tx_alt_ft} ft` : ""}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}
