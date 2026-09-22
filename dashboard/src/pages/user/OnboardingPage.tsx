import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { Notice } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { LocationPrivacyBadge } from "../../components/LocationPrivacyControl";
import type { LocationPrivacySource } from "../../types";

type OwnedNode = {
  node_id: string;
  node_ref: string | null;
  name: string;
  status: string;
  last_heartbeat: string | null;
  is_synthetic: boolean;
  rx_lat: number | null;
  rx_lon: number | null;
  frequency: number | null;
  location_private: boolean;
  location_privacy_source: LocationPrivacySource;
};

export default function OnboardingPage() {
  const [nodes, setNodes] = useState<OwnedNode[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const n = await api.myNodes();
      setNodes(Array.isArray(n) ? n : []);
    } catch (e: any) {
      setError(e?.message || "Failed to load");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  if (loading) return <div className="empty-state">Loading…</div>;

  const header = (
    <div className="page-header">
      <h1>Connect your node</h1>
      <p>
        Enter your email address in your node&rsquo;s setup and we will send you a link. Click it and the
        node joins your account.
      </p>
    </div>
  );
  // Fetched once, so a failure always means nothing loaded.
  if (error) {
    return (
      <>
        {header}
        <Notice tone="error" onRetry={refresh}>
          Could not load your nodes: {error}
        </Notice>
      </>
    );
  }

  return (
    <>
      {header}

      <div className="stats-grid">
        <StatCard label="Owned Nodes" value={nodes.length} tone="accent" />
        <StatCard
          label="Online Now"
          value={nodes.filter((n) => n.status && n.status !== "disconnected" && n.status !== "never_connected").length}
        />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>My nodes</h3>
        </div>
        <div className="card-body">
          {nodes.length === 0 ? (
            <div className="empty-state">
              No nodes yet. A node appears here once you click the link we mail you when you set it up.
            </div>
          ) : (
            <DataTable
              headers={["Node ID", "Node ref", "Status", "Frequency", "Location", "Last heartbeat"]}
              count={nodes.length}
            >
              {nodes.map((n) => {
                const online = n.status && n.status !== "disconnected" && n.status !== "never_connected";
                return (
                  <tr key={n.node_id}>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>{n.node_id}</td>
                    <td style={{ fontFamily: "monospace", fontSize: 12 }}>
                      {n.node_ref ?? "—"}{" "}
                      <LocationPrivacyBadge isPrivate={n.location_private} />
                    </td>
                    <td>
                      <span className={`badge ${online ? "online" : "offline"}`}>
                        {online ? "Online" : n.status === "never_connected" ? "Never connected" : "Offline"}
                      </span>
                    </td>
                    <td>{n.frequency ? `${(n.frequency / 1e6).toFixed(2)} MHz` : "—"}</td>
                    <td>
                      {n.rx_lat != null && n.rx_lon != null
                        ? `${n.rx_lat.toFixed(3)}, ${n.rx_lon.toFixed(3)}`
                        : "—"}
                    </td>
                    <td>{n.last_heartbeat ? new Date(n.last_heartbeat).toLocaleString() : "—"}</td>
                  </tr>
                );
              })}
            </DataTable>
          )}
        </div>
      </div>
    </>
  );
}
