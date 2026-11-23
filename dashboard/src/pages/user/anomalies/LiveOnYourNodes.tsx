import { useMemo } from "react";
import { Link } from "react-router-dom";

import { api } from "../../../api/client";
import { DataTable } from "../../../components/DataTable";
import { LocationPrivacyBadge } from "../../../components/LocationPrivacy";
import { FetchNotice, nothingLoaded } from "../../../components/Notice";
import { StatCard } from "../../../components/StatCard";
import { usePolling } from "../../../hooks/usePolling";
import type { AircraftFrame, OwnedNode } from "../../../types";
import { DASH, formatDuration } from "../../../utils/format";
import { ownedNodePage } from "../../../utils/nodes";
import { AnomalyTypeBadge, anomalyTypeLabel } from "./AnomalyTypeBadge";
import { liveAnomalies, type LiveAnomaly } from "./liveAnomalies";
import "./anomalies.css";

/** The page's own period, so the two halves of it move together. */
const REFRESH_MS = 10_000;

const HEADERS = ["Aircraft", "Type", "Your nodes", "Alt (ft)", "Speed (kt)", "Last seen"];

const EMPTY = "None of your nodes sees an anomalous aircraft right now.";

function reading(value: unknown, format: (n: number) => string): string {
  return typeof value === "number" && Number.isFinite(value) ? format(value) : DASH;
}

/** The feed's `seen`, seconds since the newest detection behind the entry. */
function lastSeen(seconds: number): string {
  return seconds < 1 ? "just now" : `${formatDuration(seconds)} ago`;
}

/** The owner's nodes behind one aircraft, each linked to its page, and how
 *  many other nodes share the solve. */
function YourNodes({ row }: { row: LiveAnomaly }) {
  return (
    <div className="anomaly-nodes">
      {row.nodes.map((node) => {
        const page = ownedNodePage(node);
        const name = node.name || node.node_ref;
        return (
          <span className="anomaly-node" key={node.node_id}>
            {page ? <Link to={page}>{name}</Link> : name}
            <LocationPrivacyBadge isPrivate={node.location_private} />
          </span>
        );
      })}
      {row.others > 0 && (
        <span className="card-note">
          with {row.others} other {row.others === 1 ? "node" : "nodes"}
        </span>
      )}
    </div>
  );
}

/**
 * What the owner's own nodes see now: the anomalous aircraft in their feed,
 * private nodes included. Mounted only while the page shows "My nodes", so the
 * feed is not asked for while nobody is looking at it.
 */
export function LiveOnYourNodes({ nodes }: { nodes: OwnedNode[] }) {
  const polled = usePolling<AircraftFrame>(() => api.myAircraft(), REFRESH_MS);
  const { data, loading, error, updatedAt } = polled;
  const live = useMemo(() => liveAnomalies(data, nodes), [data, nodes]);

  const title = (
    <h2 className="section-title">
      Live on your nodes
      <span className="card-note">private nodes included</span>
    </h2>
  );

  if (loading) {
    return (
      <>
        {title}
        <div className="card">
          <DataTable headers={HEADERS} count={0} loading />
        </div>
      </>
    );
  }
  if (nothingLoaded(polled)) {
    return (
      <>
        {title}
        <FetchNotice polled={polled} what="the aircraft your nodes see" />
      </>
    );
  }

  const { rows, byType, nodesSeeing } = live;
  const flags = byType.reduce((sum, { count }) => sum + count, 0);

  return (
    <>
      {title}
      <FetchNotice polled={polled} what="the aircraft your nodes see" />

      <div className="stats-grid">
        <StatCard label="Anomalous Aircraft" value={rows.length} tone="error" sub="your nodes see now" />
        <StatCard
          label="Your Nodes Seeing One"
          value={nodesSeeing}
          unit={`of ${nodes.length}`}
          tone="accent"
          sub="an anomalous aircraft in view"
        />
        <StatCard
          label="Most Common Type"
          value={byType.length > 0 ? anomalyTypeLabel(byType[0].type) : DASH}
          size="small"
        />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Anomalous Aircraft on Your Nodes</h3>
          <span className="card-aside">
            {/* As the event table's: the notice is above the tiles. */}
            {error && <span className="badge warning">stale</span>}
            <span className="card-note">
              {updatedAt ? `Updated ${updatedAt.toLocaleTimeString()}` : "Auto-refreshes every 10s"}
            </span>
          </span>
        </div>
        {rows.length > 0 && (
          <div className="anomaly-by-type">
            <ul className="anomaly-type-counts" aria-label="Anomalous aircraft by type">
              {byType.map(({ type, count }) => (
                <li className="anomaly-type-count" key={type}>
                  <AnomalyTypeBadge type={type} />
                  <span className="anomaly-type-count-value">{count}</span>
                </li>
              ))}
            </ul>
            {flags > rows.length && (
              <span className="card-note">An aircraft flagged for more than one type counts under each.</span>
            )}
          </div>
        )}
        <DataTable headers={HEADERS} count={rows.length} empty={EMPTY}>
          {rows.map((row) => {
            const { aircraft } = row;
            const flight = aircraft.flight?.trim();
            return (
              <tr key={aircraft.hex}>
                <td>
                  <div className="anomaly-aircraft">
                    <span className="mono">{aircraft.hex}</span>
                    {flight && <span className="card-note">{flight}</span>}
                  </div>
                </td>
                <td>
                  <div className="anomaly-types">
                    {row.types.map((type) => (
                      <AnomalyTypeBadge key={type} type={type} />
                    ))}
                  </div>
                </td>
                <td>
                  <YourNodes row={row} />
                </td>
                <td className="anomaly-reading">{reading(aircraft.alt_baro, (ft) => Math.round(ft).toLocaleString())}</td>
                <td className="anomaly-reading">{reading(aircraft.gs, (kt) => kt.toFixed(0))}</td>
                <td className="anomaly-reading">{reading(aircraft.seen, lastSeen)}</td>
              </tr>
            );
          })}
        </DataTable>
      </div>
    </>
  );
}
