import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../../api/client";
import { FetchNotice, Notice, nothingLoaded } from "../../components/Notice";
import { AddressCard, DeclarationCard, UnprotectedNotice } from "../../components/PolledRadar";
import { useAuth } from "../../context/AuthContext";
import { usePolling } from "../../hooks/usePolling";
import { useRadarCheck } from "../../hooks/useRadarCheck";
import type { OwnedNode, OwnedPolledRadar, PolledRadarLiveness } from "../../types";
import { formatMHz } from "../../utils/format";

// The poller writes liveness on every change, so a few of its polls at most
// pass before the owner sees one.
const REFRESH_MS = 10_000;

const LIVENESS: Record<PolledRadarLiveness, { words: string; badge: string }> = {
  pending: { words: "Waiting for its first answer", badge: "info" },
  streaming: { words: "Streaming", badge: "online" },
  stalled: { words: "Answering, but sending no new frames", badge: "warning" },
  unreachable: { words: "Not answering", badge: "offline" },
};

type OwnedRadar = OwnedNode & { polled: OwnedPolledRadar };

export default function RadarPage() {
  const { nodeRef = "" } = useParams();
  // A route change is another radar, including a move or removal in flight.
  return <Radar key={nodeRef} nodeRef={nodeRef} />;
}

/** The owner's own polled radar: how the server's polls of it are going,
 *  moving it to a new address and removing it. Here rather than on the node's
 *  detail page, which has nothing to show until the radar's first frame. */
function Radar({ nodeRef }: { nodeRef: string }) {
  const { polledRadarRegistration } = useAuth();
  const navigate = useNavigate();
  // The owner's list is the one place a radar's ref and its node_id, which
  // the owner's own calls take, appear together.
  const polled = usePolling(
    () =>
      api.myNodes().then(
        (nodes): OwnedRadar | null =>
          (Array.isArray(nodes) ? nodes : []).find((n: OwnedNode) => n.node_ref === nodeRef && n.polled) ?? null,
      ),
    REFRESH_MS,
    nodeRef,
  );
  const nodeId = polled.data?.node_id ?? "";
  const move = useRadarCheck((address) => api.probePolledRadarAddress(nodeId, address));
  const [moved, setMoved] = useState<string | null>(null);
  const [confirmingRemoval, setConfirmingRemoval] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removalError, setRemovalError] = useState<string | null>(null);

  const header = (
    <div className="page-header">
      <h1>{nodeRef}</h1>
      <p>A stock blah2 radar, polled at the address you gave.</p>
    </div>
  );
  if (polled.loading) return <div className="empty-state">Loading…</div>;
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="your radar" />
      </>
    );
  }
  const radar = polled.data;
  if (!radar) {
    return (
      <>
        {header}
        <div className="empty-state">Radar not found</div>
      </>
    );
  }
  const { probe, busy, refusal } = move;
  // A state this page has no words for is shown as the poller wrote it.
  const liveness = LIVENESS[radar.polled.liveness] ?? { words: radar.polled.liveness, badge: "plain" };

  async function confirmMove() {
    const address = probe?.address ?? null;
    const landed = await move.confirm(
      (raw, fingerprint) => api.movePolledRadar(nodeId, { address: raw, fingerprint }),
      "Could not move the radar. Try again.",
    );
    if (!landed) return;
    move.reset();
    setMoved(address);
    polled.refresh();
  }

  async function remove() {
    setRemoving(true);
    setRemovalError(null);
    try {
      await api.releaseNode(nodeId);
      navigate("/onboarding");
    } catch (e) {
      setRemovalError((e as Error).message || "Could not remove the radar. Try again.");
      setRemoving(false);
    }
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="your radar" />

      <div className="card">
        <div className="card-header">
          <h3>Your radar</h3>
        </div>
        <div className="card-body card-stack">
          <table className="kv-table">
            <tbody>
              <tr>
                <td>Address</td>
                <td className="mono">{radar.polled.address}</td>
              </tr>
              <tr>
                <td>Liveness</td>
                <td>
                  <span className={`badge ${liveness.badge}`}>{liveness.words}</span>
                </td>
              </tr>
              <tr>
                <td>Protection</td>
                <td>{radar.polled.unprotected ? "None" : "Password-protected"}</td>
              </tr>
              <tr>
                <td>Review</td>
                <td>{radar.polled.trust_state === "graduated" ? "Reviewed" : "On probation"}</td>
              </tr>
              <tr>
                <td>Frequency</td>
                <td className="mono">{formatMHz(radar.frequency)}</td>
              </tr>
            </tbody>
          </table>
          {radar.polled.trust_state !== "graduated" && (
            <p className="card-note">
              On probation it is on your nodes list and map, and stays out of the network&rsquo;s solves, the archive
              and the public map until an administrator has reviewed it.
            </p>
          )}
          <p className="card-note">
            <Link to={`/nodes/${nodeRef}`}>Detections and trust →</Link>
          </p>
        </div>
      </div>

      {/* The server takes no moves where it takes no registrations. */}
      {polledRadarRegistration &&
        (!probe ? (
          <AddressCard
            title="Change address"
            address={move.address}
            busy={busy}
            refusal={refusal?.message ?? null}
            onChange={move.setAddress}
            onCheck={() => {
              setMoved(null);
              move.check();
            }}
          >
            {moved && (
              <p className="card-note" role="status">
                Moved to {moved}.
              </p>
            )}
          </AddressCard>
        ) : (
          <>
            <DeclarationCard
              probe={probe}
              busy={busy}
              refusal={refusal?.at === "check" ? refusal.message : null}
              onCheckAgain={move.check}
              onChangeAddress={move.changeAddress}
            />
            {!probe.protected && <UnprotectedNotice going="move" />}
            <div className="card">
              <div className="card-header">
                <h3>Move your radar</h3>
              </div>
              <div className="card-body card-stack">
                <p className="card-note">
                  Another host or port puts it on probation until an administrator reviews it. The same host and port
                  with a new password changes only how we reach it.
                </p>
                {move.changed && (
                  <p className="card-note">The radar&rsquo;s pins have changed: check them above before moving it.</p>
                )}
                {refusal?.at === "confirm" && <Notice>{refusal.message}</Notice>}
                <div className="btn-row">
                  <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={confirmMove}>
                    {busy === "confirming" ? "Moving…" : "Move radar here"}
                  </button>
                </div>
              </div>
            </div>
          </>
        ))}

      <div className="card">
        <div className="card-header">
          <h3>Remove</h3>
        </div>
        <div className="card-body stack">
          <p className="muted">
            We stop polling it and forget its address and password. What it has already sent stays. To add it again,
            register it as a new radar.
          </p>
          {removalError && <Notice tone="error">{removalError}</Notice>}
          {confirmingRemoval ? (
            <>
              <p>
                <strong>Remove this radar?</strong>
              </p>
              <div className="btn-row">
                <button type="button" className="btn btn-danger" onClick={remove} disabled={removing}>
                  {removing ? "Removing…" : "Yes, remove it"}
                </button>
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    setConfirmingRemoval(false);
                    setRemovalError(null);
                  }}
                  disabled={removing}
                >
                  Keep it
                </button>
              </div>
            </>
          ) : (
            <div className="btn-row">
              <button type="button" className="btn btn-secondary" onClick={() => setConfirmingRemoval(true)}>
                Remove this radar
              </button>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
