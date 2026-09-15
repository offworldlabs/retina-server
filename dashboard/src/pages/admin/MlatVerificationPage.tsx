import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import { fmt } from "../../utils/format";

const REFRESH_MS = 5000;

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
    <div className="stats-grid" style={{ marginBottom: 16 }}>
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
  const { data, loading, error } = usePolling(
    () => Promise.all([api.mlatVerification(), api.mlatAccuracy()]),
    REFRESH_MS,
  );

  if (loading) return <div className="empty-state">Loading…</div>;
  if (error)   return <div className="empty-state" style={{ color: "var(--error)" }}>Error: {error.message}</div>;

  const [verification, accuracy] = data ?? [null, null];
  const v = verification ?? {};
  const a = accuracy ?? {};
  const matchThresh = v.match_threshold_km;

  return (
    <>
      <div className="page-header">
        <h1>MLAT Verification</h1>
        <p>Solver-vs-truth accuracy across all matched aircraft — auto-refreshes every 5 s</p>
      </div>

      {/* ── Latest snapshot ───────────────────────────────────────── */}
      <h2 style={{ fontSize: 16, marginTop: 24, marginBottom: 12 }}>Latest snapshot</h2>
      <div className="stats-grid" style={{ marginBottom: 16 }}>
        <StatCard label="Solves"          value={(v.n_solves ?? 0).toLocaleString()} />
        <StatCard label="Matched to truth" value={(v.n_matched ?? 0).toLocaleString()}
                  sub={matchThresh ? `≤ ${matchThresh} km from ground truth` : undefined} />
        <StatCard label="Match rate"      value={fmt(v.match_rate_pct, 1)} unit="%" />
      </div>

      {v.position && <ErrorRow label="Position error" stats={v.position} unit="km" />}
      {v.velocity && <ErrorRow label="Velocity error" stats={v.velocity} unit="ms" />}
      {v.altitude && <ErrorRow label="Altitude error" stats={v.altitude} unit="m" decimals={0} />}

      {/* ── Rolling accuracy ──────────────────────────────────────── */}
      <h2 style={{ fontSize: 16, marginTop: 32, marginBottom: 12 }}>
        Rolling accuracy
        <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: "normal", marginLeft: 8 }}>
          last {(a.n_samples ?? 0).toLocaleString()} matched samples
        </span>
      </h2>

      {a.n_samples > 0 ? (
        <>
          <div className="stats-grid" style={{ marginBottom: 16 }}>
            <StatCard label="Overall mean"   value={fmt(a.mean_km)}   unit="km" />
            <StatCard label="Overall median" value={fmt(a.median_km)} unit="km" />
            <StatCard label="Overall p95"    value={fmt(a.p95_km)}    unit="km" />
            <StatCard label="Overall max"    value={fmt(a.max_km)}    unit="km" />
          </div>

          {a.normal_only?.n_samples > 0 && (
            <>
              <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 8 }}>
                Normal-only (excludes spoofed/anomalous aircraft —
                {" "}{a.normal_only.n_samples.toLocaleString()} samples):
              </div>
              <div className="stats-grid" style={{ marginBottom: 16 }}>
                <StatCard label="Mean"   value={fmt(a.normal_only.mean_km)}   unit="km" />
                <StatCard label="Median" value={fmt(a.normal_only.median_km)} unit="km" />
                <StatCard label="p95"    value={fmt(a.normal_only.p95_km)}    unit="km" />
                <StatCard label="Max"    value={fmt(a.normal_only.max_km)}    unit="km" />
              </div>
            </>
          )}

          {a.good_geometry?.n_samples > 0 && (
            <>
              <div style={{ fontSize: 13, color: "var(--text-muted)", marginBottom: 8 }}>
                Good-geometry only (bistatic angle &lt; {a.good_geometry.bistatic_angle_threshold_deg}° —
                {" "}{a.good_geometry.n_samples.toLocaleString()} samples):
              </div>
              <div className="stats-grid" style={{ marginBottom: 16 }}>
                <StatCard label="Mean"   value={fmt(a.good_geometry.mean_km)}   unit="km" />
                <StatCard label="Median" value={fmt(a.good_geometry.median_km)} unit="km" />
                <StatCard label="p95"    value={fmt(a.good_geometry.p95_km)}    unit="km" />
                <StatCard label="Max"    value={fmt(a.good_geometry.max_km)}    unit="km" />
              </div>
            </>
          )}

          <h3 style={{ fontSize: 14, marginTop: 24, marginBottom: 8 }}>By node count</h3>
          <NodeBreakdownTable byNodeCount={a.by_node_count ?? {}} />
        </>
      ) : (
        <div className="empty-state">No matched samples in the rolling window yet.</div>
      )}
    </>
  );
}
