import { useState, type ReactNode } from "react";
import { api } from "../../../api/client";
import type { ConfigVersion } from "../../../types";
import { DASH, formatMHz } from "../../../utils/format";
import { RecordCard, useRecord } from "./RecordCard";
import { When } from "./When";
import { measure } from "./values";

/** How many versions the card asks for, which is the route's default. A
 *  history that fills it may go further back than the card can say. */
export const CONFIG_HISTORY_LIMIT = 50;

/** Versions drawn before the rest wait behind a button: most nodes hold a
 *  handful, and one that has been reconfigured fifty times should not push
 *  its own history off the page. */
const SHOWN_FIRST = 5;

function position(lat: number | null, lon: number | null): string {
  if (lat == null || lon == null || !Number.isFinite(lat) || !Number.isFinite(lon)) return DASH;
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

interface Field {
  label: string;
  /** Every column the value is drawn from: a position has changed when either
   *  of its coordinates has. */
  keys: (keyof ConfigVersion)[];
  show: (c: ConfigVersion) => ReactNode;
  mono?: boolean;
}

/** One entry per value a configuration version holds, which is every column
 *  of node_configs: so a version always differs from the one before it in at
 *  least one of these. Positions to five places, about a metre. */
const FIELDS: Field[] = [
  { label: "Carrier frequency", keys: ["fc_hz"], show: (c) => formatMHz(c.fc_hz) },
  { label: "Sample rate", keys: ["fs_hz"], show: (c) => formatMHz(c.fs_hz) },
  { label: "RX position", keys: ["rx_lat", "rx_lon"], show: (c) => position(c.rx_lat, c.rx_lon), mono: true },
  { label: "RX altitude", keys: ["rx_alt_ft"], show: (c) => measure(c.rx_alt_ft, "ft", 1) },
  { label: "TX position", keys: ["tx_lat", "tx_lon"], show: (c) => position(c.tx_lat, c.tx_lon), mono: true },
  { label: "TX altitude", keys: ["tx_alt_ft"], show: (c) => measure(c.tx_alt_ft, "ft", 1) },
  { label: "TX callsign", keys: ["tx_callsign"], show: (c) => c.tx_callsign || DASH },
  { label: "Beam width", keys: ["beam_width_deg"], show: (c) => measure(c.beam_width_deg, "°", 1) },
  { label: "Beam azimuth", keys: ["beam_azimuth_deg"], show: (c) => measure(c.beam_azimuth_deg, "°", 1) },
  { label: "Max range", keys: ["max_range_km"], show: (c) => measure(c.max_range_km, "km", 1) },
  { label: "CPI", keys: ["cpi_s"], show: (c) => measure(c.cpi_s, "s", 3) },
  { label: "Delay tolerance", keys: ["delay_tolerance_us"], show: (c) => measure(c.delay_tolerance_us, "µs", 2) },
  { label: "Doppler tolerance", keys: ["doppler_tolerance_hz"], show: (c) => measure(c.doppler_tolerance_hz, "Hz", 2) },
];

/** Compared as stored rather than as drawn, so a change finer than the format
 *  shows is still marked. An absent key and a null are the same nothing. */
function changed(field: Field, version: ConfigVersion, before: ConfigVersion): boolean {
  return field.keys.some((key) => (version[key] ?? null) !== (before[key] ?? null));
}

function Version({ version, before, complete }: { version: ConfigVersion; before?: ConfigVersion; complete: boolean }) {
  const inForce = version.superseded_at == null;
  const moved = before ? FIELDS.filter((f) => changed(f, version, before)) : [];
  let summary: string;
  if (before) {
    summary = moved.length
      ? `Changed from version ${before.version}: ${moved.map((f) => f.label).join(", ")}.`
      : `The same values as version ${before.version}.`;
  } else {
    summary = complete
      ? "The earliest configuration since this node joined your account."
      : "The oldest version loaded. Earlier versions are not shown.";
  }
  return (
    <li className="node-config-version">
      <div className="node-config-head">
        <h4 className="node-config-title">Version {version.version}</h4>
        {inForce && <span className="badge online">In force</span>}
      </div>
      <p className="node-config-summary">{summary}</p>
      <dl className="node-config-fields">
        <div className="node-config-field">
          <dt>In force from</dt>
          <dd><When at={version.created_at} /></dd>
        </div>
        <div className="node-config-field">
          <dt>Until</dt>
          <dd>{inForce ? "Now" : <When at={version.superseded_at} />}</dd>
        </div>
        {FIELDS.map((field) => {
          const isChanged = moved.includes(field);
          return (
            <div key={field.label} className={isChanged ? "node-config-field is-changed" : "node-config-field"}>
              <dt>{field.label}</dt>
              <dd>
                {field.mono ? <span className="mono">{field.show(version)}</span> : field.show(version)}
                {isChanged && <span className="node-records-sr"> (changed)</span>}
              </dd>
            </div>
          );
        })}
      </dl>
    </li>
  );
}

function Versions({ versions }: { versions: ConfigVersion[] }) {
  const [showAll, setShowAll] = useState(false);
  // Newest first whatever order they arrive in: each is compared with the one
  // after it, so the order is what makes a mark true.
  const sorted = [...versions].sort((a, b) => b.version - a.version);
  // Whether the oldest loaded is the earliest the caller is sent. Versions
  // count from 1, and a history shorter than the limit asked for is all there is.
  const oldest = sorted[sorted.length - 1];
  const complete = oldest?.version === 1 || sorted.length < CONFIG_HISTORY_LIMIT;
  const hidden = showAll ? 0 : Math.max(0, sorted.length - SHOWN_FIRST);
  const shown = hidden ? sorted.slice(0, SHOWN_FIRST) : sorted;

  if (!sorted.length) {
    return <p className="node-records-note">No configuration recorded since this node joined your account.</p>;
  }
  return (
    <>
      <ol className="node-config-versions">
        {shown.map((version, i) => (
          <Version key={version.version} version={version} before={sorted[i + 1]} complete={complete} />
        ))}
      </ol>
      {sorted.length > SHOWN_FIRST && (
        <button
          type="button"
          className="btn btn-secondary btn-sm node-config-more"
          aria-expanded={showAll}
          onClick={() => setShowAll((all) => !all)}
        >
          {showAll
            ? "Show the newest versions only"
            : `Show ${hidden} older version${hidden === 1 ? "" : "s"}`}
        </button>
      )}
    </>
  );
}

/** Every configuration the node has run under since it joined the caller's
 *  account, newest first, each value that moved from the version before it
 *  marked. */
export function ConfigHistoryCard({ nodeId, refreshKey, onGone }: { nodeId: string; refreshKey: number; onGone: () => void }) {
  const polled = useRecord(() => api.myNodeConfigHistory(nodeId, CONFIG_HISTORY_LIMIT), nodeId, refreshKey);
  const versionsOf = (data: { versions: ConfigVersion[] } | null) =>
    Array.isArray(data?.versions) ? data.versions : [];
  return (
    <RecordCard
      title="Configuration history"
      what="the configuration history"
      polled={polled}
      onGone={onGone}
      aside={(data) => {
        const count = versionsOf(data).length;
        if (!count) return null;
        return (
          <span className="card-note">
            {count >= CONFIG_HISTORY_LIMIT
              ? `Newest ${CONFIG_HISTORY_LIMIT} versions`
              : `${count} version${count === 1 ? "" : "s"}`}
          </span>
        );
      }}
    >
      {(data) => <Versions versions={versionsOf(data)} />}
    </RecordCard>
  );
}
