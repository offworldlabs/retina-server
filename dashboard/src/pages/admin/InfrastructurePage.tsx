import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../../api/client";
import { FetchNotice } from "../../components/Notice";
import { usePolling } from "../../hooks/usePolling";
import { useChartTheme, type ChartTheme } from "../../utils/chartTheme";

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

function pct(n: number | null | undefined, decimals = 1): string {
  if (n === null || n === undefined) return "—";
  return `${Number(n).toFixed(decimals)}%`;
}

function duration(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

function ago(iso: string | null): string {
  if (!iso) return "unknown";
  const at = Date.parse(iso);
  // A timestamp the API never promised to parse would render as "NaN min ago".
  if (Number.isNaN(at)) return "unknown";
  const s = Math.floor((Date.now() - at) / 1000);
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function barColour(value: number | null): string {
  if (value === null) return "var(--text-muted)";
  return value > 90 ? "var(--error)" : value > 70 ? "var(--warning)" : "var(--success)";
}

// Checks and regions are only ever UP, DOWN, or something in between (a
// region's CHECKING, a check's MIXED/UNKNOWN); the third bucket reads as a
// warning rather than as fully down.
function badgeClass(status: string): string {
  if (status === "UP") return "online";
  if (status === "DOWN") return "offline";
  return "warning";
}

function UsageBar({ label, value }: { label: string; value: number | null }) {
  const width = value === null ? 0 : Math.min(100, value);
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
        <span>{label}</span>
        <span style={{ color: "var(--text-muted)" }}>{pct(value)}</span>
      </div>
      <div style={{ height: 8, background: "var(--bg-secondary)", borderRadius: 4, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${width}%`, background: barColour(value), borderRadius: 4 }} />
      </div>
    </div>
  );
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
          <span style={{ fontWeight: 600 }}>{pct(check.uptime_30d, 2)}</span>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 8 }}>
          {Object.entries(check.regions).map(([name, r]) => (
            <span key={name} className={`badge ${badgeClass(r.status)}`} title={`since ${ago(r.since)}`}>
              {name} {r.status}
            </span>
          ))}
        </div>
        <div style={{ color: "var(--text-muted)" }}>
          {check.last_outage
            ? `Last outage: ${duration(check.last_outage.duration_seconds)}, ${ago(check.last_outage.ended_at)} (${check.last_outage.region})`
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
        <UsageBar label="CPU" value={droplet.cpu_pct} />
        <UsageBar label="Memory" value={droplet.memory_pct} />
        <UsageBar label="Disk" value={droplet.disk_pct} />
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
            <div className="stat-card">
              <div className="stat-label">Checks up</div>
              <div className="stat-value">
                {snap.checks.filter((c) => c.status === "UP").length} <span style={{ fontSize: 13, color: "var(--text-muted)" }}>/ {snap.checks.length}</span>
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-label">Droplets ({snap.tag})</div>
              <div className="stat-value">{snap.droplets.length}</div>
            </div>
            <div className={`stat-card ${snap.errors.length > 0 ? "error" : ""}`}>
              <div className="stat-label">Degraded items</div>
              <div className="stat-value">{snap.errors.length}</div>
            </div>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16, marginBottom: 16 }}>
            {snap.checks.map((c) => <CheckCard key={c.id} check={c} />)}
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 16, marginBottom: 16 }}>
            {snap.droplets.map((d, i) => (
              <DropletCard key={d.id} droplet={d} colour={theme.series[i % theme.series.length]} theme={theme} />
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
