import { Link } from "react-router-dom";

import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { LocationPrivacyBadge } from "../../components/LocationPrivacyControl";
import { useAuth } from "../../context/AuthContext";
import { useFetch } from "../../hooks/usePolling";
import { StatusBadge } from "../../components/StatusBadge";
import { formatMHz } from "../../utils/format";
import { isOnline } from "../../utils/nodes";
import type { LocationPrivacySource } from "../../types";

type OwnedNode = {
  node_id: string;
  node_ref: string | null;
  name: string | null;
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
  const { polledRadarRegistration } = useAuth();
  const polled = useFetch(() => api.myNodes().then((n): OwnedNode[] => (Array.isArray(n) ? n : [])));
  const { loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const header = (
    <div className="page-header">
      <h1>Connect your node</h1>
      <p>
        Enter your email address in your node&rsquo;s setup and we will send you a link. Click it and the
        node joins your account.
      </p>
      {polledRadarRegistration && (
        <p>
          Running stock 30hours/blah2? <Link to="/radars/new">Add a stock blah2 radar</Link> by its address.
        </p>
      )}
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

  const nodes = polled.data ?? [];

  return (
    <>
      {header}

      <div className="stats-grid">
        <StatCard label="Owned Nodes" value={nodes.length} tone="accent" />
        <StatCard
          label="Online Now"
          value={nodes.filter((n) => isOnline(n.status)).length}
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
              headers={["Node ref", "Status", "Frequency", "Location", "Last heartbeat"]}
              count={nodes.length}
            >
              {nodes.map((n) => (
                <tr key={n.node_id}>
                  <td className="mono">
                    {n.node_ref ?? "—"}{" "}
                    <LocationPrivacyBadge isPrivate={n.location_private} />
                  </td>
                  <td>
                    <StatusBadge status={n.status} />
                  </td>
                  <td>{formatMHz(n.frequency)}</td>
                  <td>
                    {n.rx_lat != null && n.rx_lon != null
                      ? `${n.rx_lat.toFixed(3)}, ${n.rx_lon.toFixed(3)}`
                      : "—"}
                  </td>
                  <td>{n.last_heartbeat ? new Date(n.last_heartbeat).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </DataTable>
          )}
        </div>
      </div>
    </>
  );
}
