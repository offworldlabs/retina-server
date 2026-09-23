import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice } from "../../components/Notice";
import { usePolling } from "../../hooks/usePolling";
import { DASH, formatRelativeTime } from "../../utils/format";

/** A matched aircraft from /api/test/mlat-verification's `tracks`. */
export interface MlatTrack {
  solver_hex: string;
  truth_hex: string | null;
  position_error_km: number;
  velocity_error_ms: number;
  altitude_error_m: number;
  n_nodes: number;
  rms_delay: number;
  rms_doppler: number;
  object_type?: string | null;
  is_anomalous?: boolean;
  max_bistatic_angle_deg?: number | null;
  timestamp_ms: number;
}

interface Solve {
  ts_ms: number;
  n_nodes: number;
  gt_error_km: number | null;
  heading_err_deg: number | null;
  rms_delay: number;
  rms_doppler: number;
  gt_hex: string | null;
}

interface SolveHistory {
  hex: string;
  window_minutes: number;
  solves?: Solve[];
  rejects_nearby?: { n: number; by_outcome?: Record<string, number> };
}

/** Good below `fair`, fair below `bad`, bad beyond; nothing for a missing value. */
function bandColour(v: number | null | undefined, fair: number, bad: number): string | undefined {
  if (v == null) return undefined;
  return v < fair ? "var(--success)" : v < bad ? "var(--warning)" : "var(--error)";
}

/** Position error against truth, in km. */
export const positionErrorColour = (km: number | null | undefined) => bandColour(km, 3, 8);

/** Heading error in degrees. Null whenever truth is near hover or the solve
 *  has no meaningful velocity. */
const headingErrorColour = (deg: number | null) => bandColour(deg, 15, 45);

/**
 * The published solves behind one MLAT marker over the stores' window, newest
 * first, and the gate rejections near its latest position. `gt_error_km` is
 * frozen at solve time against the nearest ground-truth point, so a change of
 * hex down the truth column is a visible re-bind of the truth match.
 */
export function SolveHistoryTable({ history }: { history: SolveHistory }) {
  const solves = history.solves ?? [];
  const rejects = history.rejects_nearby;
  return (
    <>
      <div style={{ padding: "4px 16px" }}>
        <p className="section-caption">
          {solves.length} published in the last {history.window_minutes} min
        </p>
        {rejects && rejects.n > 0 && (
          <p className="section-caption" title="Gate-rejected solves within 10 km of the latest published solve">
            Rejected nearby:{" "}
            {Object.entries(rejects.by_outcome ?? {})
              .map(([k, v]) => `${k.replace(/^rejected_|^n2_/, "")} ${v}`)
              .join(", ") || rejects.n}
          </p>
        )}
      </div>
      {/* Up to 500 rows, so it scrolls within the card rather than pushing the
          aircraft list off the page. Units sit in the cells because the header
          style upper-cases µs into MS. */}
      <div style={{ maxHeight: 480, overflowY: "auto" }}>
        <DataTable
          headers={["Age", "Nodes", "Truth error", "Heading error", "RMS delay", "RMS Doppler", "Truth"]}
          count={solves.length}
          empty={`No published solves in the last ${history.window_minutes} min.`}
        >
          {solves.map((s, i) => (
            <tr key={`${s.ts_ms}-${i}`}>
              <td style={{ whiteSpace: "nowrap" }}>{formatRelativeTime(s.ts_ms / 1000)}</td>
              <td>{s.n_nodes}</td>
              <td style={{ color: positionErrorColour(s.gt_error_km) }}>
                {s.gt_error_km != null ? `${s.gt_error_km.toFixed(2)} km` : DASH}
              </td>
              <td style={{ color: headingErrorColour(s.heading_err_deg) }}>
                {s.heading_err_deg != null ? `${s.heading_err_deg}°` : DASH}
              </td>
              <td>{s.rms_delay} µs</td>
              <td>{s.rms_doppler} Hz</td>
              <td>{s.gt_hex || DASH}</td>
            </tr>
          ))}
        </DataTable>
      </div>
    </>
  );
}

/** One aircraft's solve history, refreshed with the page. */
export function MlatSolveHistory({ hex, track, every }: { hex: string; track?: MlatTrack; every: number }) {
  const polled = usePolling(() => api.mlatHistory(hex) as Promise<SolveHistory>, every, hex);
  // Keyed on the hex, so a new selection never shows the previous one's rows.
  const history = polled.data?.hex === hex ? polled.data : null;
  return (
    <div className="card">
      <div className="card-header">
        <h3>
          Solve history for {hex}
          {/* The snapshot's match, which the solves' own truth column below
              can differ from when the match has re-bound. */}
          {track?.truth_hex ? ` (matched to ${track.truth_hex} in the latest snapshot)` : ""}
        </h3>
      </div>
      {/* What loaded for another aircraft is not on screen, so for this one it
          counts as never loaded. */}
      <FetchNotice polled={{ ...polled, updatedAt: history ? polled.updatedAt : null }} what="the solve history" />
      {history ? (
        <SolveHistoryTable history={history} />
      ) : (
        polled.pending && <div className="empty-state">Loading…</div>
      )}
    </div>
  );
}
