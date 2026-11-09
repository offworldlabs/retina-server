import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { HttpError } from "@retina/shared";

import { api } from "../../api/client";
import { DataTable, type ColumnHint } from "../../components/DataTable";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { Sheet } from "../../components/Sheet";
import { useNodeIds } from "../../hooks/useNodeIds";
import { usePolling } from "../../hooks/usePolling";
import type { AdminFleetPerformance, AdminPerformanceRow, NodeTower } from "../../types";
import { DASH } from "../../utils/format";
import { PerformancePanel } from "../user/nodeDetail/PerformancePanel";
import { formatSystemConstant, rankStatusText, TOWER_STATUS } from "../user/contribution/figures";
import "./FleetPerformancePage.css";

/** The figures change once a day, with the nightly analysis. */
const POLL_MS = 300_000;

const COLUMNS = ["Node", "Rank", "S_n", "Ceiling", "T_n", "Residual MAD", "Opportunities", "Tower", "Reason", "Details"];
const HINTS: readonly (ColumnHint | undefined)[] = [
  "title",
  undefined,
  undefined,
  "secondary",
  "secondary",
  undefined,
  "secondary",
  "secondary",
  undefined,
  undefined,
];

function towerText(tower: NodeTower | null): string {
  if (!tower?.status) return DASH;
  const label = TOWER_STATUS[tower.status];
  return tower.callsign ? `${label} (${tower.callsign})` : label;
}

function fixed(value: number | null, places: number, unit: string): string {
  return value == null ? DASH : `${value.toFixed(places)} ${unit}`;
}

/** One node's full block, as its owner reads it, plus the bearing chart. The
 *  figures change once a day, so it is polled as slowly as the table. A node
 *  with no performance file answers 404, which the panel words itself. */
function NodePerformanceSheet({ nodeId, onClose }: { nodeId: string; onClose: () => void }) {
  const polled = usePolling(() => api.adminNodePerformance(nodeId), POLL_MS, nodeId);
  const missing = polled.error instanceof HttpError && polled.error.status === 404;

  return (
    <Sheet open onClose={onClose} variant="bottom" title={<span className="mono">{nodeId}</span>} className="fleet-performance-sheet">
      {missing ? (
        <PerformancePanel performance={null} />
      ) : (
        <>
          <FetchNotice polled={polled} what="this node's performance" />
          {polled.loading && <div className="empty-state">Loading…</div>}
          {polled.data && <PerformancePanel performance={polled.data} />}
        </>
      )}
    </Sheet>
  );
}

export default function FleetPerformancePage() {
  const [opened, setOpened] = useState<string | null>(null);
  const polled = usePolling<AdminFleetPerformance>(() => api.adminFleetPerformance(), POLL_MS);
  const refs = useNodeIds();
  const refOf = useMemo(() => new Map(Object.entries(refs ?? {}).map(([ref, id]) => [id, ref])), [refs]);

  const { data, loading } = polled;
  const rows: AdminPerformanceRow[] = Array.isArray(data?.nodes) ? data.nodes : [];
  const measured = rows.find((row) => row.window)?.window;

  return (
    <>
      <div className="page-header">
        <h1>Fleet Performance</h1>
        <p>What each node measures against ADS-B traffic. Flagged nodes sit at the bottom with the reason.</p>
      </div>
      <FetchNotice polled={polled} what="the performance figures" />

      {!nothingLoaded(polled) && (
        <div className="card fleet-performance-card">
          <div className="card-header">
            <h3>Nodes</h3>
            {measured && (
              <span className="card-note">
                {measured[0]} to {measured[1]}
              </span>
            )}
          </div>
          <DataTable
            headers={COLUMNS}
            hints={HINTS}
            count={rows.length}
            loading={loading}
            empty="No performance figures yet. The first ones land after the nightly analysis."
          >
            {rows.map((row) => {
              const ref = refOf.get(row.node_id);
              const flagged = row.rank_status === "config_suspect";
              return (
                <tr key={row.node_id} className={flagged ? "fleet-performance-flagged" : undefined}>
                  <td className="cell-primary" data-label="Node">
                    {ref ? (
                      <Link to={`/nodes/${encodeURIComponent(ref)}`} className="mono detail-link">
                        {row.node_id}
                      </Link>
                    ) : (
                      <span className="mono">{row.node_id}</span>
                    )}
                  </td>
                  <td data-label="Rank">{row.rank == null ? DASH : `#${row.rank}`}</td>
                  <td data-label="S_n">
                    {formatSystemConstant(row.system_constant_db, row.system_constant_sigma_db, row.system_constant_bound)}
                  </td>
                  <td data-label="Ceiling">{row.pd_ceiling == null ? DASH : row.pd_ceiling >= 1 ? "Not seen" : `${Math.round(row.pd_ceiling * 100)}%`}</td>
                  <td data-label="T_n">{fixed(row.threshold_db, 1, "dB")}</td>
                  <td data-label="Residual MAD">
                    {fixed(row.tag_delay_residual_mad_us, 2, "µs")}
                    {row.delay_residual_source === "claims" && " (claims)"}
                  </td>
                  <td data-label="Opportunities">{row.opportunities == null ? DASH : row.opportunities.toLocaleString()}</td>
                  <td data-label="Tower">{towerText(row.tower)}</td>
                  <td data-label="Reason" className={flagged ? "fleet-performance-reason" : undefined}>
                    {row.rank == null ? rankStatusText(row.rank_status) : DASH}
                  </td>
                  <td data-label="Details">
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      aria-label={`Show what ${row.node_id} detects`}
                      onClick={() => setOpened(row.node_id)}
                    >
                      Open
                    </button>
                  </td>
                </tr>
              );
            })}
          </DataTable>
        </div>
      )}
      {opened && <NodePerformanceSheet key={opened} nodeId={opened} onClose={() => setOpened(null)} />}
    </>
  );
}
