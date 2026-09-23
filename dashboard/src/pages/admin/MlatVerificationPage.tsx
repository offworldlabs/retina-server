import { useEffect, useRef } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { DASH, fmt, formatRelativeTime } from "../../utils/format";
import { MlatSolveHistory, positionErrorColour, type MlatTrack } from "./MlatSolveHistory";

const REFRESH_MS = 5000;

/**
 * The matched aircraft in the latest snapshot, worst position error first.
 * Each links to its own solve history, by address so a selection can be sent
 * to someone.
 */
function TracksTable({ tracks, selected }: { tracks: MlatTrack[]; selected: string | null }) {
  const rows = [...tracks].sort((a, b) => b.position_error_km - a.position_error_km);
  return (
    <DataTable
      headers={["Aircraft", "Truth", "Position error", "Velocity error", "Altitude error", "Nodes",
                "RMS delay", "RMS Doppler", "Bistatic angle", "Type", "Solved"]}
      count={rows.length}
      empty="No aircraft matched to truth in the latest snapshot."
    >
      {rows.map((t) => (
        <tr key={t.solver_hex} className={t.solver_hex === selected ? "selected" : undefined}>
          <td>
            <Link to={`?hex=${encodeURIComponent(t.solver_hex)}`}
                  aria-current={t.solver_hex === selected ? "true" : undefined}>
              {t.solver_hex}
            </Link>
          </td>
          <td style={{ whiteSpace: "nowrap" }}>{t.truth_hex ?? DASH}</td>
          <td style={{ color: positionErrorColour(t.position_error_km) }}>{fmt(t.position_error_km, 2)} km</td>
          <td>{fmt(t.velocity_error_ms, 1)} m/s</td>
          <td>{fmt(t.altitude_error_m, 0)} m</td>
          <td>{t.n_nodes}</td>
          <td>{t.rms_delay} µs</td>
          <td>{t.rms_doppler} Hz</td>
          <td>{t.max_bistatic_angle_deg != null ? `${t.max_bistatic_angle_deg}°` : DASH}</td>
          <td>{t.is_anomalous ? `${t.object_type ?? "aircraft"}, anomalous` : t.object_type ?? "aircraft"}</td>
          <td style={{ whiteSpace: "nowrap" }}>{formatRelativeTime(t.timestamp_ms / 1000)}</td>
        </tr>
      ))}
    </DataTable>
  );
}

function ErrorRow({ label, stats, unit, decimals = 2 }: {
  label: string;
  stats: { mean_km?: number; median_km?: number; p95_km?: number; max_km?: number;
           mean_ms?: number; median_ms?: number; p95_ms?: number;
           mean_m?: number;  median_m?: number;  p95_m?: number };
  unit: string;
  decimals?: number;
}) {
  const mean   = (stats as any)[`mean_${unit}`];
  const median = (stats as any)[`median_${unit}`];
  const p95    = (stats as any)[`p95_${unit}`];
  const max    = (stats as any)[`max_${unit}`];
  return (
    <div className="stats-grid">
      <StatCard label={`${label} — mean`}   value={fmt(mean,   decimals)} unit={unit === "km" ? "km" : unit === "ms" ? "m/s" : "m"} />
      <StatCard label={`${label} — median`} value={fmt(median, decimals)} unit={unit === "km" ? "km" : unit === "ms" ? "m/s" : "m"} />
      <StatCard label={`${label} — p95`}    value={fmt(p95,    decimals)} unit={unit === "km" ? "km" : unit === "ms" ? "m/s" : "m"} />
      {max !== undefined && (
        <StatCard label={`${label} — max`}  value={fmt(max,    decimals)} unit={unit === "km" ? "km" : unit === "ms" ? "m/s" : "m"} />
      )}
    </div>
  );
}

function NodeBreakdownTable({ byNodeCount }: {
  byNodeCount: Record<string, { n_samples: number; mean_km: number; median_km: number; p95_km: number; max_km: number }>;
}) {
  const rows = Object.entries(byNodeCount).sort(([a], [b]) => Number(a) - Number(b));
  if (!rows.length) return <div className="empty-state">No samples yet.</div>;
  return (
    <DataTable headers={["Nodes", "Samples", "Mean (km)", "Median (km)", "p95 (km)", "Max (km)"]} count={rows.length}>
      {rows.map(([nc, s]) => (
        <tr key={nc}>
          <td>{nc}</td>
          <td>{s.n_samples.toLocaleString()}</td>
          <td>{fmt(s.mean_km)}</td>
          <td>{fmt(s.median_km)}</td>
          <td>{fmt(s.p95_km)}</td>
          <td>{fmt(s.max_km)}</td>
        </tr>
      ))}
    </DataTable>
  );
}

export default function MlatVerificationPage() {
  const polled = usePolling(
    () => Promise.all([api.mlatVerification(), api.mlatAccuracy()]),
    REFRESH_MS,
  );
  const { data, loading } = polled;
  const [searchParams] = useSearchParams();
  // Lower case, as the solver mints it and mlat-history echoes it.
  const selected = searchParams.get("hex")?.toLowerCase() ?? null;

  // The history sits above the table, so a row chosen far down it is followed
  // up to what it opened. Also once the page loads, for a link that arrives
  // with an aircraft already chosen.
  const historyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (selected && !loading) historyRef.current?.scrollIntoView?.({ block: "start" });
  }, [selected, loading]);

  if (loading) return <div className="empty-state">Loading…</div>;

  const [verification, accuracy] = data ?? [null, null];
  const v = verification ?? {};
  const a = accuracy ?? {};
  const matchThresh = v.match_threshold_km;

  const header = (
    <div className="page-header">
      <h1>MLAT Verification</h1>
      <p>Solver-vs-truth accuracy across all matched aircraft — auto-refreshes every 5 s</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="MLAT verification" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="MLAT verification" />

      {/* ── Latest snapshot ───────────────────────────────────────── */}
      <h2 className="section-title">Latest snapshot</h2>
      <div className="stats-grid">
        <StatCard label="Solves"          value={(v.n_solves ?? 0).toLocaleString()} />
        <StatCard label="Matched to truth" value={(v.n_matched ?? 0).toLocaleString()}
                  sub={matchThresh ? `≤ ${matchThresh} km from ground truth` : undefined} />
        {/* Against the distinct aircraft behind the solves, which is the rate's
            own denominator: several solver cycles for one aircraft count once. */}
        <StatCard label="Match rate"      value={fmt(v.match_rate_pct, 1)} unit="%"
                  sub={v.n_unique_aircraft != null ? `${v.n_matched ?? 0} of ${v.n_unique_aircraft} aircraft` : undefined} />
      </div>

      {v.position && <ErrorRow label="Position error" stats={v.position} unit="km" />}
      {v.velocity && <ErrorRow label="Velocity error" stats={v.velocity} unit="ms" />}
      {v.altitude && <ErrorRow label="Altitude error" stats={v.altitude} unit="m" decimals={0} />}

      {/* ── Per aircraft ──────────────────────────────────────────── */}
      <h2 className="section-title">
        Matched aircraft
        <span className="card-note">choose one for its solve history</span>
      </h2>
      {selected && (
        <div ref={historyRef}>
          <MlatSolveHistory
            hex={selected}
            track={(v.tracks ?? []).find((t: MlatTrack) => t.solver_hex === selected)}
            every={REFRESH_MS}
          />
        </div>
      )}
      <div className="card">
        <TracksTable tracks={v.tracks ?? []} selected={selected} />
      </div>

      {/* ── Rolling accuracy ──────────────────────────────────────── */}
      <h2 className="section-title">
        Rolling accuracy
        <span className="card-note">
          last {(a.n_samples ?? 0).toLocaleString()} matched samples
        </span>
      </h2>

      {a.n_samples > 0 ? (
        <>
          <div className="stats-grid">
            <StatCard label="Overall mean"   value={fmt(a.mean_km)}   unit="km" />
            <StatCard label="Overall median" value={fmt(a.median_km)} unit="km" />
            <StatCard label="Overall p95"    value={fmt(a.p95_km)}    unit="km" />
            <StatCard label="Overall max"    value={fmt(a.max_km)}    unit="km" />
          </div>

          {a.normal_only?.n_samples > 0 && (
            <>
              <div className="section-caption">
                Normal-only (excludes spoofed/anomalous aircraft —
                {" "}{a.normal_only.n_samples.toLocaleString()} samples):
              </div>
              <div className="stats-grid">
                <StatCard label="Mean"   value={fmt(a.normal_only.mean_km)}   unit="km" />
                <StatCard label="Median" value={fmt(a.normal_only.median_km)} unit="km" />
                <StatCard label="p95"    value={fmt(a.normal_only.p95_km)}    unit="km" />
                <StatCard label="Max"    value={fmt(a.normal_only.max_km)}    unit="km" />
              </div>
            </>
          )}

          {a.good_geometry?.n_samples > 0 && (
            <>
              <div className="section-caption">
                Good-geometry only (bistatic angle &lt; {a.good_geometry.bistatic_angle_threshold_deg}° —
                {" "}{a.good_geometry.n_samples.toLocaleString()} samples):
              </div>
              <div className="stats-grid">
                <StatCard label="Mean"   value={fmt(a.good_geometry.mean_km)}   unit="km" />
                <StatCard label="Median" value={fmt(a.good_geometry.median_km)} unit="km" />
                <StatCard label="p95"    value={fmt(a.good_geometry.p95_km)}    unit="km" />
                <StatCard label="Max"    value={fmt(a.good_geometry.max_km)}    unit="km" />
              </div>
            </>
          )}

          <div className="card">
            <div className="card-header"><h3>By node count</h3></div>
            <NodeBreakdownTable byNodeCount={a.by_node_count ?? {}} />
          </div>
        </>
      ) : (
        <div className="empty-state">No matched samples in the rolling window yet.</div>
      )}
    </>
  );
}
