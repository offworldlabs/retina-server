import type { ReactNode } from "react";
import { measure } from "../../../components/nodeOwner/records/values";
import type { OwnedNode } from "../../../types";
import { DASH, formatMHz } from "../../../utils/format";

function finite(n: unknown): n is number {
  return typeof n === "number" && Number.isFinite(n);
}

/** One end of the pair: the coordinates to five places, about a metre, and
 *  the altitude where the node declared one. */
function Position({ lat, lon, altFt }: { lat?: number | null; lon?: number | null; altFt?: number | null }) {
  const at = finite(lat) && finite(lon) ? `${lat.toFixed(5)}, ${lon.toFixed(5)}` : null;
  const alt = finite(altFt) ? measure(altFt, "ft", 1) : null;
  if (!at && !alt) return <>{DASH}</>;
  return (
    <span className="node-detail-position">
      <span className="mono">{at ?? DASH}</span>
      {/* Read out between the two; the flex row draws the gap instead. */}
      {alt && <>{" "}<span>/ {alt}</span></>}
    </span>
  );
}

function sampleRate(hz: number | null | undefined): string {
  return finite(hz) && hz > 0 ? `${(hz / 1e6).toFixed(2)} MSps` : DASH;
}

interface Row {
  label: string;
  value: ReactNode;
}

function Card({ rows }: { rows: Row[] }) {
  return (
    <div className="card">
      <div className="card-header"><h3>RF Configuration</h3></div>
      <div className="card-body">
        <table className="kv-table node-detail-rf">
          <tbody>
            {rows.map(({ label, value }) => (
              <tr key={label}>
                <td>{label}</td>
                <td>{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** The owner's node as configured: the true positions, and every RF setting
 *  the owner routes carry, a private node's included. */
export function OwnerRfCard({ node }: { node: OwnedNode }) {
  const location = node.location ?? ({} as Partial<OwnedNode["location"]>);
  const rf = node.rf ?? ({} as Partial<OwnedNode["rf"]>);
  return (
    <Card
      rows={[
        { label: "Center Frequency", value: formatMHz(node.frequency) },
        { label: "Sample Rate", value: sampleRate(node.sample_rate) },
        { label: "RX Location", value: <Position lat={location.rx_lat} lon={location.rx_lon} altFt={location.rx_alt_ft} /> },
        { label: "TX Location", value: <Position lat={location.tx_lat} lon={location.tx_lon} altFt={location.tx_alt_ft} /> },
        { label: "TX Callsign", value: rf.tx_callsign || DASH },
        { label: "Beam Width", value: measure(rf.beam_width_deg, "°", 1) },
        { label: "Beam Azimuth", value: measure(rf.beam_azimuth_deg, "°", 1) },
        { label: "Max Range", value: measure(rf.max_range_km, "km", 1) },
        { label: "CPI", value: measure(rf.cpi_s, "s", 3) },
        { label: "Delay Tolerance", value: measure(rf.delay_tolerance_us, "µs", 2) },
        { label: "Doppler Tolerance", value: measure(rf.doppler_tolerance_hz, "Hz", 2) },
      ]}
    />
  );
}

/** The node as /api/radar/nodes publishes it, receiver position included as
 *  published. */
export function PublicRfCard({ nodeInfo }: { nodeInfo: any }) {
  const location = nodeInfo.location || {};
  return (
    <Card
      rows={[
        { label: "Center Frequency", value: formatMHz(nodeInfo.frequency) },
        { label: "Sample Rate", value: sampleRate(nodeInfo.sample_rate) },
        { label: "RX Location", value: <Position lat={location.rx_lat} lon={location.rx_lon} altFt={location.rx_alt_ft} /> },
        { label: "TX Location", value: <Position lat={location.tx_lat} lon={location.tx_lon} altFt={location.tx_alt_ft} /> },
      ]}
    />
  );
}
