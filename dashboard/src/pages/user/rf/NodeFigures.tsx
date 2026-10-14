import { StatCard } from "../../../components/StatCard";
import type { OwnedNode } from "../../../types";
import { DASH, formatMHz } from "../../../utils/format";
import { detectionCount } from "../../../utils/nodes";
import { type AnalyticsSummary, averageSnr, finite, type NetworkNode, type PublicLocation } from "./feeds";

/**
 * One node's readings in the page's terms, whichever feed they came from: the
 * owner's own data for an owned node, private or not, and the published
 * figures for anyone else's.
 */

interface Position {
  lat: number;
  lon: number;
  altFt: number | null;
}

export interface NodeFigures {
  ref: string | null;
  /** dB. */
  avgSnr: number | null;
  /** Carrier, Hz. */
  frequency: number | null;
  /** Hz. */
  sampleRate: number | null;
  totalFrames: number | null;
  detections: number | null;
  rx: Position | null;
  tx: Position | null;
  /** How far the receiver shown may be from the true one; null where it is
   *  the true one. */
  rxWithinKm: number | null;
  /** True while the analytics behind the counts have not answered yet. */
  pending: boolean;
  /** The analytics answered and have nothing for this node yet. */
  noAnalytics: boolean;
}

function position(lat: unknown, lon: unknown, altFt: unknown): Position | null {
  const la = finite(lat);
  const lo = finite(lon);
  return la === null || lo === null ? null : { lat: la, lon: lo, altFt: finite(altFt) };
}

function counts(summary: AnalyticsSummary | null) {
  return {
    avgSnr: averageSnr(summary),
    totalFrames: finite(summary?.metrics?.total_frames),
    detections: summary ? detectionCount(summary) : null,
  };
}

/** An owned node: the true geometry and configuration from the owner's list,
 *  the counts from the owner's analytics. `summary` is undefined without an
 *  answer, and null when the answer is that there is nothing yet; `pending`
 *  tells an answer on its way from one that failed. */
export function ownedFigures(
  node: OwnedNode,
  summary: AnalyticsSummary | null | undefined,
  pending = summary === undefined,
): NodeFigures {
  const location = node.location;
  return {
    ref: node.node_ref,
    ...counts(summary ?? null),
    frequency: finite(node.frequency) ?? finite(summary?.detection_area?.center_freq),
    sampleRate: finite(node.sample_rate),
    rx: position(location?.rx_lat ?? node.rx_lat, location?.rx_lon ?? node.rx_lon, location?.rx_alt_ft),
    tx: position(location?.tx_lat, location?.tx_lon, location?.tx_alt_ft),
    rxWithinKm: null,
    pending,
    noAnalytics: summary === null,
  };
}

/** Anyone else's node, as the network publishes it. */
export function networkFigures(node: NetworkNode): NodeFigures {
  const location: PublicLocation = node.location;
  const within = finite(location.location_uncertainty_km);
  return {
    ref: node.ref,
    ...counts(node.summary),
    frequency: node.frequency ?? finite(node.summary?.detection_area?.center_freq),
    sampleRate: node.sampleRate,
    rx: position(location.rx_lat, location.rx_lon, location.rx_alt_ft),
    tx: position(location.tx_lat, location.tx_lon, location.tx_alt_ft),
    rxWithinKm: within !== null && within > 0 ? within : null,
    pending: false,
    noAnalytics: node.summary === null,
  };
}

/** A count's reading: an ellipsis while it is on its way, a dash for none. */
function reading(figures: NodeFigures, value: number | null, show: (n: number) => string): string {
  if (figures.pending) return "…";
  return value === null ? DASH : show(value);
}

const decibels = (decimals: number) => (n: number) => `${n.toFixed(decimals)} dB`;
const whole = (n: number) => n.toLocaleString();

export function NodeStats({ figures }: { figures: NodeFigures }) {
  return (
    <div className="stats-grid">
      <StatCard
        label="Average SNR"
        value={reading(figures, figures.avgSnr, decibels(1))}
        tone="accent"
        sub={figures.noAnalytics ? "No analytics for this node yet" : undefined}
      />
      <StatCard label="Frequency" value={formatMHz(figures.frequency)} tone="success" />
      <StatCard label="Total Frames" value={reading(figures, figures.totalFrames, whole)} />
      <StatCard label="Detections" value={reading(figures, figures.detections, whole)} tone="warning" />
    </div>
  );
}

function place(at: Position | null, withinKm: number | null = null): string {
  if (!at) return DASH;
  const parts = [`${at.lat.toFixed(4)}, ${at.lon.toFixed(4)}`];
  if (at.altFt !== null) parts.push(`${Math.round(at.altFt).toLocaleString()} ft`);
  if (withinKm !== null) parts.push(`approximate, within ${withinKm.toFixed(1)} km`);
  return parts.join(" · ");
}

export function NodeDetails({ figures, owned }: { figures: NodeFigures; owned: boolean }) {
  const rate =
    figures.detections !== null && figures.totalFrames
      ? `${((figures.detections / figures.totalFrames) * 100).toFixed(1)}%`
      : DASH;
  return (
    <div className="card">
      <div className="card-header">
        <h3>Node RF Details</h3>
        <span className="card-note">{owned ? "From your node's own data" : "As the network publishes it"}</span>
      </div>
      <div className="card-body">
        <table className="kv-table">
          <tbody>
            <tr><td>Node ref</td><td className="mono">{figures.ref ?? DASH}</td></tr>
            <tr><td>Frequency</td><td>{formatMHz(figures.frequency)}</td></tr>
            <tr><td>Sample Rate</td><td>{formatMHz(figures.sampleRate)}</td></tr>
            <tr><td>Average SNR</td><td>{reading(figures, figures.avgSnr, decibels(2))}</td></tr>
            <tr><td>Total Frames Processed</td><td>{reading(figures, figures.totalFrames, whole)}</td></tr>
            <tr><td>Detection Rate</td><td>{figures.pending ? "…" : rate}</td></tr>
            <tr><td>RX Location</td><td>{place(figures.rx, figures.rxWithinKm)}</td></tr>
            <tr><td>TX Location</td><td>{place(figures.tx)}</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  );
}
