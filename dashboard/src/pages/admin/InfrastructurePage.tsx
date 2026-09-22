import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../../api/client";
import { FetchNotice } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { UsageBar } from "../../components/UsageBar";
import { usePolling } from "../../hooks/usePolling";
import { seriesColour, useChartTheme, type ChartTheme } from "../../utils/chartTheme";
import { DASH, formatDuration, formatPercent, formatRelativeTime } from "../../utils/format";

// DigitalOcean's checks run every minute and the backend caches for one, so
// polling faster than this only re-reads the cache.
const REFRESH_MS = 60_000;

type Region = { status: string; since: string | null; uptime_30d: number | null };
type Outage = { region: string; started_at: string; ended_at: string; duration_seconds: number };
type Check = {
  id: string; name: string; target: string; enabled: boolean; status: string;
  uptime_30d: number | null; regions: Record<string, Region>; last_outage: Outage | null;
};
type Point = number[];
type Droplet = {
  id: number; name: string; vcpus: number; memory_mb: number; disk_gb: number; status: string;
  cpu_pct: number | null; memory_pct: number | null; disk_pct: number | null;
  series: { cpu: Point[]; memory: Point[]; disk: Point[] };
};
type Snapshot = {
  configured: boolean; reason: string | null; fetched_at: number; tag: string;
  stale: boolean; checks: Check[]; droplets: Droplet[]; errors: string[];
};

// Checks and regions are only ever UP, DOWN, or something in between (a
// region's CHECKING, a check's MIXED/UNKNOWN); the third bucket reads as a
// warning rather than as fully down.
function badgeClass(status: string): string {
  if (status === "UP") return "online";
  if (status === "DOWN") return "offline";
  return "warning";
}

function Spark({ series, colour, theme }: { series: Point[]; colour: string; theme: ChartTheme }) {
  const data = series.map(([t, v]) => ({ t, v }));
  // An off droplet, a missing metrics agent and a broken metrics API all arrive
  // as an empty series; a blank chart would read as zero usage.
  if (data.length === 0) {
    return (
      <div style={{ height: 72, display: "flex", alignItems: "center", fontSize: 12, color: "var(--text-muted)" }}>
        no data
      </div>
    );
  }
  return (
    <div style={{ height: 72 }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
          <XAxis dataKey="t" hide />
          <YAxis domain={[0, 100]} hide />
          <Tooltip
            contentStyle={theme.tooltip}
            formatter={(v) => `${v}%`}
            labelFormatter={(t) => new Date(Number(t) * 1000).toLocaleString()}
          />
          <Area type="monotone" dataKey="v" stroke={colour} fill={colour} fillOpacity={0.2} isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function Series({ label, series, colour, theme }: { label: string; series: Point[]; colour: string; theme: ChartTheme }) {
  return (
    <>
      <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", margin: "8px 0 2px" }}>{label}</div>
      <Spark series={series} colour={colour} theme={theme} />
    </>
  );
}

function CheckCard({ check }: { check: Check }) {
  return (
    <div className="card">
      <div className="card-header">
        <h3>{check.name}</h3>
        <span style={{ display: "flex", gap: 6 }}>
          {/* A disabled check reports nothing, so its status is stale rather than good. */}
          {!check.enabled && <span className="badge warning">disabled</span>}
          <span className={`badge ${badgeClass(check.status)}`}>{check.status}</span>
        </span>
      </div>
      <div style={{ padding: "0 20px 16px", fontSize: 13 }}>
        <div style={{ color: "var(--text-muted)", marginBottom: 8, wordBreak: "break-all" }}>{check.target}</div>
        <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
          <span>30-day uptime</span>
          <span style={{ fontWeight: 600 }}>{formatPercent(check.uptime_30d, 2)}</span>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
          {Object.entries(check.regions).map(([name, r]) => {
            const since = formatRelativeTime(r.since);
            return (
              <span key={name} className={`badge ${badgeClass(r.status)}`} title={since === DASH ? undefined : `since ${since}`}>
                {name} {r.status}
              </span>
            );
          })}
        </div>
        <div style={{ color: "var(--text-muted)" }}>
          {check.last_outage
            ? `Last outage: ${formatDuration(check.last_outage.duration_seconds)}, ${formatRelativeTime(check.last_outage.ended_at)} (${check.last_outage.region})`
            : "No outage recorded"}
        </div>
      </div>
    </div>
  );
}

function DropletCard({ droplet, colour, theme }: { droplet: Droplet; colour: string; theme: ChartTheme }) {
  return (
    <div className="card">
      <div className="card-header">
        <h3>{droplet.name}</h3>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {droplet.vcpus} vCPU · {Math.round(droplet.memory_mb / 1024)} GB · {droplet.disk_gb} GB disk
          </span>
          {/* Only `active` is running; every other power state reads as offline. */}
          <span className={`badge ${droplet.status === "active" ? "online" : "offline"}`}>{droplet.status}</span>
        </span>
      </div>
      <div style={{ padding: "0 20px 16px" }}>
        <UsageBar label="CPU" value={formatPercent(droplet.cpu_pct)} pct={droplet.cpu_pct} />
        <UsageBar label="Memory" value={formatPercent(droplet.memory_pct)} pct={droplet.memory_pct} />
        <UsageBar label="Disk" value={formatPercent(droplet.disk_pct)} pct={droplet.disk_pct} />
        <Series label="CPU, last 24 h" series={droplet.series.cpu} colour={colour} theme={theme} />
        <Series label="Memory, last 24 h" series={droplet.series.memory} colour={colour} theme={theme} />
        <Series label="Disk, last 24 h" series={droplet.series.disk} colour={colour} theme={theme} />
      </div>
    </div>
  );
}

export default function InfrastructurePage() {
  const polled = usePolling<Snapshot>(api.adminInfrastructure, REFRESH_MS);
  const { data: snap, loading } = polled;
  const theme = useChartTheme();

  if (loading) return <div className="empty-state">Loading…</div>;

  const header = (
    <div className="page-header">
      <h1>Infrastructure</h1>
      <p>Uptime checks and droplet health from DigitalOcean. A view only: the alerts email whether or not this page loads.</p>
      {/* The backend caches for a minute, so the reading can lag the refresh. */}
      {snap && (
        <p style={{ fontSize: 12, color: "var(--text-muted)" }}>
          as of {new Date(snap.fetched_at * 1000).toLocaleTimeString()}{" "}
          {/* The route serves the previous build when a refresh overruns its deadline. */}
          {snap.stale && (
            <span className="badge warning" title="The last refresh timed out; showing the previous snapshot">stale</span>
          )}
        </p>
      )}
    </div>
  );
  // A refresh that failed costs the reading its freshness, not the page.
  const notice = <FetchNotice polled={polled} what="the infrastructure snapshot" />;
  if (!snap) {
    return (
      <>
        {header}
        {notice}
      </>
    );
  }

  return (
    <>
      {header}
      {notice}

      {!snap.configured ? (
        <div className="empty-state">
          Not configured on this server: {snap.reason}. See claude-shared docs/runbooks/uptime-monitoring.md.
        </div>
      ) : (
        <>
          <div className="stats-grid">
            <StatCard
              label="Checks up"
              value={snap.checks.filter((c) => c.status === "UP").length}
              unit={`/ ${snap.checks.length}`}
            />
            <StatCard label={`Droplets (${snap.tag})`} value={snap.droplets.length} />
            <StatCard
              label="Degraded items"
              value={snap.errors.length}
              tone={snap.errors.length > 0 ? "error" : undefined}
            />
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16, marginBottom: 16 }}>
            {snap.checks.map((c) => <CheckCard key={c.id} check={c} />)}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16, marginBottom: 16 }}>
            {snap.droplets.map((d, i) => (
              <DropletCard key={d.id} droplet={d} colour={seriesColour(theme, i)} theme={theme} />
            ))}
          </div>

          {snap.errors.length > 0 && (
            <div className="card">
              <div className="card-header"><h3>Degraded</h3></div>
              <ul style={{ padding: "0 20px 16px 36px", fontSize: 13, color: "var(--text-muted)" }}>
                {snap.errors.map((e) => <li key={e}>{e}</li>)}
              </ul>
            </div>
          )}
        </>
      )}
    </>
  );
}
