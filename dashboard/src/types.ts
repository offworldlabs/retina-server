/* ------------------------------------------------------------------ */
/*  Shared API response types — RETINA dashboard                      */
/* ------------------------------------------------------------------ */

import type { Aircraft } from "./pages/map/types";

/** Which ends of a node's bistatic pair have coordinates. Orthogonal to a
 *  node's `status` (liveness): a node can be actively detecting and still
 *  be anything but "positioned". */
export type PositionStatus = "positioned" | "missing_rx" | "missing_tx" | "missing_both";

/* ---- Polled radars ---- */

/** Whether a polled radar's data reaches the solve, the archive and the public
 *  map ("graduated") or only its own analytics and tracker ("probation"). */
export type PolledRadarTrust = "probation" | "graduated";

/** One human decision about a node, from node_events. */
export interface NodeEvent {
  kind: "registered" | "graduated" | "returned_to_probation" | "removed";
  /** The radar's epoch the decision applied to. */
  epoch: number | null;
  /** `admin:<email>` for an administrator, or the owner's account id. */
  actor: string;
  at: string;
}

/** Whose polled radar it is. An account an administrator made for its address
 *  reads not signed in until its first sign-in. */
export interface PolledRadarOwner {
  email: string | null;
  signed_in: boolean;
}

/** How the server's polls of a radar are going, as the poller last wrote it. */
export type PolledRadarLiveness = "pending" | "streaming" | "stalled" | "unreachable";

/** A stock blah2 radar the server polls, as administrators see it. */
export interface PolledRadar {
  node_id: string;
  node_ref: string;
  /** `host:port`: the operator's address, shown only to admins and the owner. */
  endpoint: string;
  liveness: PolledRadarLiveness;
  last_frame_at: string | null;
  /** Moves, back on probation, whenever what the radar declares may have come
   *  from another box. A decision applies to one epoch. */
  epoch: number;
  trust_state: PolledRadarTrust;
  /** Newest first. */
  events: NodeEvent[];
  owner: PolledRadarOwner | null;
}

export interface PolledRadarListing {
  probation_enabled: boolean;
  /** Whether this server polls radars, so whether one can be connected here. */
  polling_enabled: boolean;
  radars: PolledRadar[];
}

/** One of a radar's two sites as its own blah2 config declares it. */
export interface RadarSite {
  latitude: number;
  longitude: number;
  altitude_m: number;
  name: string | null;
}

/** What a probe found at the address an owner typed. `fs_hz` and `cpi_s` are
 *  as they will be registered: the radar's own, or the defaults where it gave
 *  none. `fingerprint` is what the owner confirms by registering. */
export interface PolledRadarProbe {
  /** Without any credentials the owner typed. */
  address: string;
  rx: RadarSite;
  tx: RadarSite;
  fc_hz: number;
  fs_hz: number;
  cpi_s: number;
  fingerprint: string;
  /** A password was given and the radar refused a read without it. */
  protected: boolean;
}

/** An administrator's check of a radar and of the address it is for. */
export interface ConnectionCheck extends PolledRadarProbe {
  /** The address as accounts are keyed on it, and whether it has one yet. */
  account: { email: string; exists: boolean };
}

export type Publication = "public" | "private";

/** A polled radar as its owner sees it, under `polled` in their node list. */
export interface OwnedPolledRadar {
  /** As the owner gave it, less any password. */
  address: string;
  unprotected: boolean;
  liveness: PolledRadarLiveness;
  trust_state: PolledRadarTrust;
}

/** The geometry of one owned node as configured, the true positions. Nulls
 *  where the node has not declared one. */
export interface OwnerNodeLocation {
  rx_lat: number | null;
  rx_lon: number | null;
  rx_alt_ft: number | null;
  tx_lat: number | null;
  tx_lon: number | null;
  tx_alt_ft: number | null;
}

/** An owned node's RF configuration beyond carrier and sample rate. Nulls
 *  where unknown. */
export interface OwnerNodeRf {
  tx_callsign: string | null;
  beam_width_deg: number | null;
  beam_azimuth_deg: number | null;
  max_range_km: number | null;
  cpi_s: number | null;
  delay_tolerance_us: number | null;
  doppler_tolerance_hz: number | null;
}

/** One node as its owner sees it: an item of /api/auth/me/nodes and the answer
 *  for a single node. Read from the live connection, or from the active
 *  config row while the node is not connected. */
export interface OwnedNode {
  node_id: string;
  node_ref: string | null;
  /** What to show: the owner's label, else the node's own name, else its ref. */
  name: string | null;
  /** The owner's own label, never published. Null when unset. */
  label: string | null;
  status: string;
  last_heartbeat: string | null;
  config_hash: string | null;
  is_synthetic: boolean;
  rx_lat: number | null;
  rx_lon: number | null;
  position_status: PositionStatus;
  /** Carrier, Hz. */
  frequency: number | null;
  /** Hz. */
  sample_rate: number | null;
  location: OwnerNodeLocation;
  rf: OwnerNodeRf;
  location_private: boolean;
  /** Null for a node nobody claimed by email, which is every node an
   *  administrator assigned and every polled radar. */
  claimed_with: string | null;
  /** Null for every node but a polled radar. */
  polled: OwnedPolledRadar | null;
}

/** What an owner may change on a node. A null or empty `name` clears the label. */
export interface OwnerNodePatch {
  name?: string | null;
  location_private?: boolean;
}

/** Unfuzzed analytics of one owned node, the keys of /api/radar/analytics/{ref}
 *  with nothing moved. The sections not typed here are read as they come. */
export interface OwnerSummary {
  node_id: string;
  node_ref: string | null;
  trust: { trust_score?: number | null; [key: string]: unknown };
  detection_area: { n_detections?: number | null; [key: string]: unknown };
  metrics: {
    total_detections?: number | null;
    total_frames?: number | null;
    total_tracks?: number | null;
    avg_snr?: number | null;
    max_snr?: number | null;
    availability_7d?: number | null;
    availability_24h?: number | null;
    [key: string]: unknown;
  };
  reputation: Record<string, unknown>;
  coverage_map: Record<string, unknown> | null;
  empirical_coverage: Record<string, unknown> | null;
  node_bias: Record<string, unknown> | null;
}

/** /api/auth/me/analytics: every owned node with analytics, private ones
 *  included, keyed by node_ref. */
export interface OwnerAnalytics {
  nodes: Record<string, OwnerSummary>;
}

/** A node's last heartbeat. Only nodes that speak the v1 protocol send one. */
export interface NodeReport {
  received_at: string;
  state: string;
  state_since: string | null;
  uptime_s: number | null;
  config_version: number | null;
  health: Record<string, unknown>;
  versions: Record<string, string | null>;
  errors: { at: string; message: string }[];
}

/** One row of a node's configuration history, newest first. */
export interface ConfigVersion {
  version: number;
  created_at: string;
  /** Null for the version in force. */
  superseded_at: string | null;
  fc_hz: number | null;
  fs_hz: number | null;
  rx_lat: number | null;
  rx_lon: number | null;
  rx_alt_ft: number | null;
  tx_lat: number | null;
  tx_lon: number | null;
  tx_alt_ft: number | null;
  tx_callsign: string | null;
  beam_width_deg: number | null;
  beam_azimuth_deg: number | null;
  max_range_km: number | null;
  cpi_s: number | null;
  delay_tolerance_us: number | null;
  doppler_tolerance_hz: number | null;
}

/** What happened to a node. A kind the console does not know yet still arrives. */
export type OwnerEventKind =
  | "registered"
  | "removed"
  | "graduated"
  | "returned_to_probation"
  | "renamed"
  | "made_private"
  | "made_public"
  | (string & {});

/** One entry of an owned node's history, newest first. The stored actor is
 *  never sent, since it can be an administrator's email. */
export interface OwnerEvent {
  kind: OwnerEventKind;
  at: string;
  actor: "you" | "owner" | "admin" | "system";
  epoch: number | null;
}

/** One five-minute bucket of detection SNR. */
export interface SnrPoint {
  t_ms: number;
  median_db: number;
  p90_db: number;
  count: number;
}

/** Per-bucket SNR of one owned node's detections. */
export interface SnrSeries {
  /** The window asked for, 1 to 72. */
  hours: number;
  bucket_s: number;
  /** The newest detection included, epoch ms. Null when there is no data. */
  through_ms: number | null;
  /** Oldest first; empty buckets are left out. */
  points: SnrPoint[];
}

/** One file in an archive listing. The key carries the node_ref, never the node_id. */
export interface ArchiveFile {
  key: string;
  size_bytes: number;
  modified: string;
}

/** A page of the archive, as /api/data/archive and the owner's own listing send it. */
export interface ArchiveListing {
  files: ArchiveFile[];
  count: number;
  total: number;
  truncated?: boolean;
}

/** One aircraft in the owner's feed frame. The map's Aircraft, plus the keys
 *  only this feed carries. */
export interface FeedAircraft extends Aircraft {
  alt_geom?: number;
  messages?: number;
}

/** One frame of /api/auth/me/aircraft, and of the owner's socket. */
export interface AircraftFrame {
  now?: number;
  messages?: number;
  aircraft: FeedAircraft[];
  detection_arcs?: Record<string, unknown>;
  detecting_nodes?: Record<string, unknown>;
  ground_truth?: Record<string, unknown>;
  ground_truth_meta?: Record<string, unknown>;
  anomaly_hexes?: string[];
}

/** One row of the leaderboard, what anyone is sent. */
export interface LeaderboardRow {
  node_ref: string;
  name: string;
  rank: number;
  detections: number;
  frames: number;
  tracks: number;
  /** Null until a whole minute has been measured. */
  availability_7d: number | null;
  avg_snr: number;
  trust_score: number;
  reputation: number;
  online: boolean;
  /** The miss counts go only to a caller with a session. */
  in_range?: number;
  detected_in_range?: number;
  missed?: number;
  miss_rate?: number;
}

/** /api/radar/leaderboard: the ranked rows and how many there are. */
export interface Leaderboard {
  leaderboard: LeaderboardRow[];
  total: number;
}

/* ---- The signed-in account ---- */

/** One browser signed in to the caller's account, from /api/auth/me/sessions.
 *  Times are epoch seconds. */
export interface AccountSession {
  id: string;
  created_at: number;
  last_used_at: number;
  expires_at: number;
  /** The server's short label for the browser, such as "Firefox on Windows". */
  user_agent: string | null;
  /** The network the browser was in, a /24 or a /48, never its address. */
  ip: string | null;
  /** The session asking. */
  current: boolean;
}

/** What deleting the account answers with: how many nodes it released. */
export interface AccountDeleted {
  ok: true;
  released: number;
}

/** A confirmation link mailed to a new sign-in address. */
export interface EmailChangeSent {
  status: "sent";
  /** The new address as the server normalised it. */
  email: string;
  /** How long the link stays open, in seconds. */
  expires_in: number;
}

/** A claim mail queued again. Never answered without one queued. */
export interface ClaimMailSent {
  status: "sent";
}

/** An open claim on a node given one of the caller's addresses, current or
 *  retired, from /api/auth/me/claims. */
export interface MyClaim {
  node_ref: string;
  /** The address the node was given, which may be a retired one. */
  email: string;
  pending: boolean;
  /** When the outstanding link lapses, in epoch seconds; null with none out. */
  expires_at: number | null;
}
