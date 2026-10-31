import { useId, type ReactNode } from "react";
import { api } from "../../../api/client";
import type { NodeReport } from "../../../types";
import { DASH, fmt, formatBytes, formatPercent } from "../../../utils/format";
import { RecordCard, useRecord } from "./RecordCard";
import { When } from "./When";
import { formatUptime, humanize, plainValue } from "./values";

/** The `.badge` tone of each state a v1 node reports. Streaming is the one
 *  healthy steady state; stalled and error are the ones an owner acts on; the
 *  rest are on the way somewhere, or held there by the server. A state the
 *  console has no tone for reads as neutral. */
const STATE_TONE = new Map([
  ["streaming", "online"],
  ["starting", "info"],
  ["stopping", "info"],
  ["paused", "info"],
  ["stalled", "warning"],
  ["error", "offline"],
]);

const SERVICE = new Map([
  ["up", { tone: "online", label: "Up" }],
  ["down", { tone: "offline", label: "Down" }],
  ["unknown", { tone: "private", label: "Unknown" }],
]);

/** A service the node watches, as a badge when it is one of the three states
 *  the contract names. */
function service(value: unknown): ReactNode {
  const known = typeof value === "string" ? SERVICE.get(value) : undefined;
  if (!known) return plainValue(value);
  return <span className={`badge ${known.tone}`}>{known.label}</span>;
}

/** Each format applies only to a value of the type the contract gives it, so a
 *  node sending something else still shows what it sent. */
const number = (format: (n: number) => string) => (value: unknown) =>
  typeof value === "number" ? format(value) : plainValue(value);

/** The readings the v1 contract names, in the order an owner checks them. The
 *  disk figure is the node's MB, read as the MiB `df -m` gives. */
const HEALTH: { key: string; label: string; show: (value: unknown) => ReactNode }[] = [
  { key: "cpu_pct", label: "CPU load", show: number((n) => formatPercent(n, 1)) },
  { key: "temp_c", label: "Temperature", show: number((n) => `${fmt(n, 1)} °C`) },
  { key: "disk_free_mb", label: "Free disk space", show: number((n) => formatBytes(n * 1024 * 1024)) },
  { key: "blah2", label: "Radar service (blah2)", show: service },
  // Left out of the beat, or sent as null, when ADS-B is disabled in the
  // node's configuration: the contract reads both as disabled, not unknown.
  { key: "adsb", label: "ADS-B receiver", show: (value) => (value == null ? "Disabled" : service(value)) },
];

const SOFTWARE: { key: string; label: string }[] = [
  { key: "owl_os", label: "Operating system (owl-os)" },
  { key: "retina_node", label: "Node software (retina-node)" },
  { key: "blah2_image", label: "Radar software (blah2)" },
  { key: "retina_tracker", label: "Tracker release" },
];

interface Row {
  key: string;
  label: string;
  value: ReactNode;
}

/** The named readings first, then anything newer the node sent, under its own
 *  key. Read as a dictionary only through own keys, so a reading called
 *  "constructor" is a reading and not Object's. */
function healthRows(health: Record<string, unknown>): Row[] {
  // A beat with no health block: nothing in it, ADS-B included.
  if (Object.keys(health).length === 0) return [];
  const has = (key: string) => Object.prototype.hasOwnProperty.call(health, key);
  const named = new Set(HEALTH.map((h) => h.key));
  const rows: Row[] = HEALTH.filter((h) => has(h.key) || h.key === "adsb").map((h) => ({
    key: h.key,
    label: h.label,
    value: h.show(has(h.key) ? health[h.key] : undefined),
  }));
  for (const [key, value] of Object.entries(health)) {
    if (!named.has(key)) rows.push({ key, label: humanize(key), value: plainValue(value) });
  }
  return rows;
}

function softwareRows(versions: Record<string, unknown>): Row[] {
  const show = (value: unknown) =>
    typeof value === "string" && value ? <span className="mono">{value}</span> : plainValue(value);
  const named = new Set(SOFTWARE.map((s) => s.key));
  const rows: Row[] = SOFTWARE.filter((s) => Object.prototype.hasOwnProperty.call(versions, s.key)).map((s) => ({
    key: s.key,
    label: s.label,
    value: show(versions[s.key]),
  }));
  for (const [key, value] of Object.entries(versions)) {
    if (!named.has(key)) rows.push({ key, label: humanize(key), value: show(value) });
  }
  return rows;
}

/** Newest first. The server keeps them in the order they arrived, and the
 *  errors of one beat share its stamp, so arrival order breaks the tie. A stamp
 *  that does not parse sorts last. */
function newestFirst(errors: NodeReport["errors"]) {
  return errors
    .map((error, index) => {
      const ms = Date.parse(error?.at);
      return { error, index, ms: Number.isNaN(ms) ? -Infinity : ms };
    })
    .sort((a, b) => (a.ms === b.ms ? b.index - a.index : b.ms - a.ms))
    .map(({ error }) => error);
}

/** The server sends an empty record for a beat that carried no block; anything
 *  that is not a record at all is read as one too, rather than walked. */
function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function Readings({ title, rows, empty }: { title: string; rows: Row[]; empty: string }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId}>
      <h4 id={headingId} className="node-records-subhead">{title}</h4>
      {rows.length ? (
        <table className="kv-table">
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td>{row.label}</td>
                <td>{row.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="node-records-empty">{empty}</p>
      )}
    </section>
  );
}

function Errors({ errors }: { errors: NodeReport["errors"] }) {
  const headingId = useId();
  const sorted = newestFirst(errors);
  return (
    <section aria-labelledby={headingId}>
      <h4 id={headingId} className="node-records-subhead">
        Errors reported
        {sorted.length > 0 && <span className="badge plain">{sorted.length}</span>}
      </h4>
      {sorted.length ? (
        <ol className="node-report-errors">
          {sorted.map((error, i) => (
            <li key={i} className="node-report-error">
              <p className="node-report-error-message">{error?.message || DASH}</p>
              <When at={error?.at} />
            </li>
          ))}
        </ol>
      ) : (
        <p className="node-records-empty">No errors reported since this node joined your account.</p>
      )}
    </section>
  );
}

function Report({ report }: { report: NodeReport }) {
  const status: Row[] = [
    { key: "received", label: "Received", value: <When at={report.received_at} /> },
    {
      key: "state",
      label: "State",
      value: report.state ? (
        <span className={`badge ${STATE_TONE.get(report.state) ?? "private"}`}>{humanize(report.state)}</span>
      ) : (
        DASH
      ),
    },
    { key: "since", label: "In this state since", value: <When at={report.state_since} /> },
    { key: "uptime", label: "Uptime", value: formatUptime(report.uptime_s) },
    { key: "config", label: "Configuration version", value: report.config_version ?? DASH },
  ];
  return (
    <div className="node-report">
      <div className="node-report-grid">
        <Readings title="Status" rows={status} empty="" />
        <Readings
          title="Health"
          rows={healthRows(asRecord(report.health))}
          empty="This report carries no health readings."
        />
        <Readings
          title="Software"
          rows={softwareRows(asRecord(report.versions))}
          empty="This report names no software versions."
        />
      </div>
      <Errors errors={Array.isArray(report.errors) ? report.errors : []} />
    </div>
  );
}

/** The node's last heartbeat, as the node itself tells it. Diagnostic: the
 *  server decides whether a node is working from the frames it receives. */
export function ReportCard({ nodeId, refreshKey, onGone }: { nodeId: string; refreshKey: number; onGone: () => void }) {
  const polled = useRecord(() => api.myNodeReport(nodeId), nodeId, refreshKey);
  return (
    <RecordCard
      title="Last report"
      what="the last report"
      polled={polled}
      onGone={onGone}
      aside={(report) => report && <span className="card-note">The node&rsquo;s own account, from its last heartbeat</span>}
    >
      {(report) =>
        report ? (
          <Report report={report} />
        ) : (
          <p className="node-records-note">
            This node has not sent a heartbeat report yet. Nodes on the v1 node software send one with every
            heartbeat; older node software and polled blah2 radars never do.
          </p>
        )
      }
    </RecordCard>
  );
}
