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

/* ---- Admin: accounts and node claims ---- */

/** An account as the API's user dict reports it, which is what deactivating
 *  and reactivating one answer with. Times are epoch seconds. */
export interface AdminAccount {
  id: string;
  email: string;
  /** Empty until the owner sets one. */
  name: string;
  role: string;
  is_superuser: boolean;
  /** False once deactivated: no link signs it in and no session resolves to it. */
  is_active: boolean;
  created_at: number;
  /** Null until its first visit. */
  last_seen_at: number | null;
}

/** An account as the console's user pages list it. */
export interface AdminUser extends AdminAccount {
  /** Whether a link sent to the address has been redeemed. An account an
   *  administrator made for a polled radar reads false until then. */
  is_verified: boolean;
  /** The nodes it owns. */
  node_count: number;
}

/** One page of accounts, newest first, and how many match in all. */
export interface AdminUserPage {
  users: AdminUser[];
  total: number;
}

/** What a node is told about its own claim. An expired link reads unclaimed. */
export type AdminClaimStatus = "owned" | "pending" | "unclaimed";

/** The claim link a node has out. Times are epoch seconds. */
export interface AdminClaimChallenge {
  /** Where the link was sent. */
  email: string;
  created_at: number;
  expires_at: number;
  /** As of the server's read; a page left open goes by expires_at. */
  expired: boolean;
}

/** A node's claim as an administrator reads it: the status the node is told,
 *  and the facts it is derived from. */
export interface AdminClaim {
  node_id: string;
  /** Null for a node with no public handle. */
  node_ref: string | null;
  status: AdminClaimStatus;
  owner: { user_id: string; email: string | null; name: string | null } | null;
  /** The address on file for the claim, if any. */
  email: string | null;
  /** Whether a link to that address was redeemed. */
  verified: boolean;
  /** Mail to that address bounced for good. */
  undeliverable: boolean;
  updated_at: number | null;
  owned_since: number | null;
  challenge: AdminClaimChallenge | null;
}

/** One account, the nodes it owns, and the claims on other nodes that carry
 *  its address: in progress, expired or bounced. */
export interface AdminUserDetail {
  user: AdminUser;
  nodes: AdminClaim[];
  claims: AdminClaim[];
}

/** One entry of the admin event log, newest first. */
export interface AdminEvent {
  /** Epoch seconds. */
  ts: number;
  category: string;
  message: string;
  severity: string;
  /** Ids the event is about, such as user_id or node_id. */
  meta?: Record<string, unknown>;
}

/* ---- Admin: the node registry beside the Mender mirror ---- */

export type AdminFleetPresence = "both" | "registry_only" | "mender_only";

/** What the console knows of a node's owner: a claim whose link was redeemed,
 *  only the site's contact address, or nothing. */
export type AdminFleetEmailState = "claimed_verified" | "contact_only" | "none";

/** How the last Mender snapshot stands. Times are epoch seconds. */
export interface AdminFleetMirror {
  /** When the server received the last snapshot; null before the first. */
  last_snapshot_at: number | null;
  /** Negative when the server clock went back past the snapshot. */
  age_s: number | null;
  /** Null when MENDER_MIRROR_MAX_AGE_S is malformed. */
  max_age_s: number | null;
  /** No snapshot, one older than max_age_s, or a malformed limit. */
  stale: boolean;
  /** Why it is stale, in the server's words; null while fresh. */
  stale_reason: string | null;
  generated_at: number | null;
  /** Who pushed it, such as "mender-auto-accept@central". */
  source: string | null;
  device_count: number | null;
  /** "api", "shadow" or "mirror"; anything else is a misconfiguration. */
  mender_source: string;
}

/** One node as the registry, its owner's claim and the Mender mirror report
 *  it. Times are epoch seconds. */
export interface AdminFleetRow {
  /** Null only for a Mender device with no node_id identity. */
  node_id: string | null;
  presence: AdminFleetPresence;
  node_ref: string | null;
  status: string | null;
  first_seen_at: number | null;
  last_seen_at: number | null;
  claim_email: string | null;
  claim_verified: boolean | null;
  contact_email: string | null;
  email_state: AdminFleetEmailState;
  mender_device_id: string | null;
  /** accepted, pending, rejected, preauthorized, ambiguous, ... */
  auth_status: string | null;
  device_type: string | null;
  artifact_name: string | null;
  inventory_updated_at: number | null;
  /** False once Mender decommissioned the device; the row is kept. */
  present: boolean | null;
  /** Known to Mender only, under a node_id with the "ret" prefix. */
  never_registered: boolean;
  /** Why registration refuses this node whatever its auth_status; null when
   *  its snapshot entry is usable. Several are joined with "; ". */
  snapshot_issue: string | null;
}

export interface AdminFleet {
  mirror: AdminFleetMirror;
  counts: Record<AdminFleetPresence, number>;
  rows: AdminFleetRow[];
}
