/* ------------------------------------------------------------------ */
/*  Archive replay: the /api/data/tracks contract and the frames the  */
/*  synthesiser hands the map instead of a WebSocket message.         */
/* ------------------------------------------------------------------ */

import type { Aircraft } from "../types";

export type Lane = "dark" | "known" | "follow";
export type NodeKind = "real" | "synthetic" | "mixed";

/** One row of GET /api/data/tracks: one published solve. */
export interface TrackRow {
  frame_ts_ms: number;
  solve_ts_ms: number;
  /** The multinode_tracks key; null in files written before the key column existed. */
  track_key: string | null;
  lane: Lane | null;
  node_kind: NodeKind | null;
  lat: number;
  lon: number;
  alt_m: number | null;
  vel_east_ms: number | null;
  vel_north_ms: number | null;
  vel_up_ms: number | null;
  n_nodes: number;
  /** Public node refs, never private ids. */
  node_refs: string[];
  adsb_hex: string | null;
  pos_sigma_m: number | null;
  rms_delay_us: number | null;
  rms_doppler_hz: number | null;
  is_anomalous: boolean | null;
  anomaly_types: string[];
}

export interface TracksResponse {
  from: number;
  to: number;
  fleet: string;
  count: number;
  truncated: boolean;
  rows: TrackRow[];
}

export interface AvailabilityHour {
  start_ms: number;
  count: number;
  unclassified: number;
}

export interface AvailabilityResponse {
  from: string;
  to: string;
  fleet: string;
  hours: AvailabilityHour[];
  total: number;
}

/**
 * A synthesised aircraft entry: multinode_to_aircraft's shape
 * (backend/services/aircraft_feed.py) after public-feed publication, which
 * renames contributing_node_ids to contributing_node_refs.
 *
 * `recent_positions`, `contributing_node_refs` and `anomaly_types` are shared
 * with the replay index and with every other frame that shows the same row:
 * read them, never edit them in place.
 */
export interface ReplayAircraft extends Aircraft {
  hex: string;
  type: "multinode_solve";
  position_source: "multinode_solve";
  multinode: true;
  /** Same value as alt_baro, as the live entry carries both. */
  alt_geom?: number;
  adsb_assisted: boolean;
  adsb_hex?: string;
  /** Archive-only: the row's measurement epoch (the "Solved" time). */
  frame_ts_ms: number;
  /** Archive-only: when the server published it. */
  solve_ts_ms: number;
}

/** One row of GET /api/data/detections: one detection a public node published. */
export interface DetectionRow {
  /** The detecting node's published identity. */
  node_ref: string;
  /** The frame's time as the node stamped it, epoch ms. */
  frame_ts_ms: number;
  delay_us: number | null;
  doppler_hz: number | null;
  snr_db: number | null;
  /** The ADS-B aircraft the detection was matched to when it was taken. */
  adsb_hex: string | null;
  /** The receiver's published position, as the live map showed it. */
  rx_lat: number | null;
  rx_lon: number | null;
  /** The transmitter's true position. */
  tx_lat: number | null;
  tx_lon: number | null;
}

export interface DetectionsResponse {
  from: number;
  /** Exclusive. */
  to: number;
  fleet: string;
  count: number;
  /** More rows matched than the route's cap; the rest, all later, were cut. */
  truncated: boolean;
  /** False when the server may already have deleted some of the window's detections from its disk, so rows may be missing. */
  local: boolean;
  /**
   * True when the server may not have archived all of the window yet: it
   * writes detections once an hour, so rows of about the last hour may be
   * missing now and come in a later answer.
   */
  pending: boolean;
  /** Sorted by frame_ts_ms, then node_ref, then delay_us. */
  rows: DetectionRow[];
}

/**
 * A detection arc a replayed frame carries: the entry the map's afterglow
 * buffer takes from `detection_arcs` (arcBuffer.ts). `ts` is the detection's
 * own time, which the arc fades from on the map's clock.
 */
export interface ReplayArc {
  hex: string;
  node_ref: string;
  /** Built from the receiver and transmitter the archive holds for this detection. */
  ambiguity_arc: [number, number][];
  delay_us: number;
  doppler_hz: number;
  alt_baro: number | null;
  /** frame_ts_ms: epoch ms. */
  ts: number;
}

/** One row of GET /api/data/adsb: one aircraft in one snapshot of the server's ADS-B picture. */
export interface AdsbRow {
  /** When the server snapshotted its ADS-B picture, epoch ms (every 10 s). */
  ts_ms: number;
  /** ICAO address, lowercase. */
  hex: string;
  lat: number;
  lon: number;
  /** Barometric altitude, feet; 0 on the ground, null when the fix had none. */
  alt_baro: number | null;
  /** Ground speed, knots. */
  gs: number | null;
  /** Track, degrees. */
  track: number | null;
  /** Callsign. */
  flight: string | null;
  /**
   * When the fix was received, epoch ms, to the whole second and never after
   * ts_ms. The snapshot holds the freshest fix the server had, which can be up
   * to 45 s older than the snapshot.
   */
  seen_ms: number;
}

export interface AdsbResponse {
  from: number;
  /** Exclusive. */
  to: number;
  fleet: string;
  count: number;
  /** More rows matched than the route's cap; the rest, all later, were cut. */
  truncated: boolean;
  /** Sorted by ts_ms, then hex. */
  rows: AdsbRow[];
}

/**
 * One point of an aircraft's trail in a frame's `ground_truth`, as the live
 * feed's ground truth carries it: [lat, lon, alt_m, ts_s]. alt_m is null when
 * the archived fix had no altitude.
 */
export type TruthPoint = [number, number, number | null, number];

/**
 * What a frame's `ground_truth_meta` says of an archived ADS-B aircraft, in
 * the fields the map's ground-truth layer reads (groundTruthFixes.ts). Never
 * anomalous: the archive holds the transponder's own report.
 */
export interface TruthMeta {
  has_adsb: true;
  adsb_callsign: string | null;
  /** From the row's ground speed; absent when it had none. */
  speed_ms?: number;
  /** The row's track; absent when it had none. */
  heading?: number;
  object_type: "aircraft";
  source: "adsb";
  /** When the shown fix was received, epoch ms: the trail's last point, never after the frame's instant. */
  seen_ms: number;
}

export interface ReplayFrame {
  /** SECONDS since epoch (= clockMs / 1000), like the live feed's `now`. */
  now: number;
  aircraft: ReplayAircraft[];
  /** [] from synthesizeFrame: the session adds the detections it holds (detectionsLoader.ts). */
  detection_arcs: ReplayArc[];
  /** Always {}. */
  detecting_nodes: Record<string, never>;
  anomaly_hexes: string[];
  /**
   * {} from synthesizeFrame: the session adds the archived ADS-B traffic of
   * the instant (adsbLoader.ts), by ICAO hex.
   */
  ground_truth: Record<string, TruthPoint[]>;
  /** {} from synthesizeFrame; with `ground_truth`, by the same hexes. */
  ground_truth_meta: Record<string, TruthMeta>;
}
