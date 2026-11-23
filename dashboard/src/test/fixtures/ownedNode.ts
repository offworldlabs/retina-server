import type { OwnedNode } from "../../types";

/** A node as its owner's list sends it, every key present. A test names only
 *  what it reads. */
export function ownedNode(overrides: Partial<OwnedNode> = {}): OwnedNode {
  const nodeId = overrides.node_id ?? "ret0a1b2c3d";
  return {
    node_id: nodeId,
    node_ref: `nde${nodeId.replace(/^[a-z]+/, "")}`,
    name: null,
    label: null,
    status: "active",
    last_heartbeat: null,
    config_hash: null,
    is_synthetic: false,
    rx_lat: null,
    rx_lon: null,
    position_status: "positioned",
    frequency: null,
    sample_rate: null,
    location: { rx_lat: null, rx_lon: null, rx_alt_ft: null, tx_lat: null, tx_lon: null, tx_alt_ft: null },
    rf: {
      tx_callsign: null,
      beam_width_deg: null,
      beam_azimuth_deg: null,
      max_range_km: null,
      cpi_s: null,
      delay_tolerance_us: null,
      doppler_tolerance_hz: null,
    },
    location_private: false,
    claimed_with: null,
    polled: null,
    ...overrides,
  };
}
