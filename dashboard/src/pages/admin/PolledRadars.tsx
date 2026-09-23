import { useState } from "react";
import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, Notice, nothingLoaded } from "../../components/Notice";
import { useFetch } from "../../hooks/usePolling";
import type { PolledRadar, PolledRadarListing, PolledRadarTrust } from "../../types";

const TRUST_LABEL: Record<PolledRadarTrust, string> = {
  probation: "On probation",
  graduated: "Graduated",
};

const DECISION_LABEL: Record<string, string> = {
  graduated: "Graduated",
  returned_to_probation: "Returned to probation",
};

function lastDecision(radar: PolledRadar): string {
  const event = radar.events[0];
  if (!event) return "—";
  const who = event.actor.replace(/^admin:/, "");
  const epoch = event.epoch === null ? "" : ` at epoch ${event.epoch}`;
  return `${DECISION_LABEL[event.kind] ?? event.kind} by ${who}${epoch}, ${new Date(event.at).toLocaleString()}`;
}

/** Polled stock-blah2 radars and the trust decision on each. A radar on
 *  probation never reaches the node list, which reads the public feed, so this
 *  is where an administrator finds one to graduate. */
export function PolledRadars() {
  const polled = useFetch<PolledRadarListing>(() => api.adminPolledRadars());
  const [refusal, setRefusal] = useState<string | null>(null);
  const [deciding, setDeciding] = useState(false);

  async function decide(radar: PolledRadar) {
    const next: PolledRadarTrust = radar.trust_state === "graduated" ? "probation" : "graduated";
    const question =
      next === "graduated"
        ? `Graduate ${radar.node_id} at epoch ${radar.epoch}? Its data will reach the solve, the archive and the public map.`
        : `Return ${radar.node_id} to probation? Its data will be held back from the solve, the archive and the public map.`;
    if (!window.confirm(question)) return;
    setRefusal(null);
    setDeciding(true);
    try {
      // The epoch on screen: the server refuses it once the radar has moved on.
      await api.setAdminPolledRadarTrust(radar.node_id, next, radar.epoch);
    } catch (e) {
      setRefusal((e as Error).message || "Could not apply the decision. Try again.");
    } finally {
      setDeciding(false);
      polled.refresh();
    }
  }

  const listing = polled.data;
  const radars = listing?.radars ?? [];
  return (
    <div className="card">
      <div className="card-header">
        <h3>Polled radars</h3>
      </div>
      <FetchNotice polled={polled} what="polled radars" />
      {refusal && <Notice>{refusal}</Notice>}
      {listing && !listing.probation_enabled && (
        <p className="card-note">
          Probation is switched off, so every polled radar already reaches the solve, the archive and the public map.
          A decision here takes effect once it is back on.
        </p>
      )}
      {!nothingLoaded(polled) && (
        <DataTable
          headers={["Node ID", "Address", "Liveness", "Epoch", "Trust", "Last decision", ""]}
          count={radars.length}
          empty="No polled radars are registered"
          loading={polled.loading}
        >
          {radars.map((radar) => {
            const action = radar.trust_state === "graduated" ? "Return to probation" : "Graduate";
            return (
              <tr key={radar.node_id}>
                <td className="mono">{radar.node_id}</td>
                <td className="mono">{radar.endpoint}</td>
                <td>{radar.liveness}</td>
                <td className="mono">{radar.epoch}</td>
                <td>
                  <span className={radar.trust_state === "graduated" ? "badge online" : "badge warning"}>
                    {TRUST_LABEL[radar.trust_state]}
                  </span>
                </td>
                <td>{lastDecision(radar)}</td>
                <td>
                  <button
                    type="button"
                    className="btn btn-secondary btn-sm"
                    // Held until the reload lands, so no row offers a decision on what it showed before.
                    disabled={deciding || polled.pending}
                    aria-label={`${action} ${radar.node_id}`}
                    onClick={() => decide(radar)}
                  >
                    {action}
                  </button>
                </td>
              </tr>
            );
          })}
        </DataTable>
      )}
    </div>
  );
}
