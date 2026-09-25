/* ------------------------------------------------------------------ */
/*  Shared API response types — RETINA dashboard                      */
/* ------------------------------------------------------------------ */

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
  kind: "graduated" | "returned_to_probation";
  /** The radar's epoch the decision applied to. */
  epoch: number | null;
  /** `admin:<email>` for an administrator. */
  actor: string;
  at: string;
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
}

export interface PolledRadarListing {
  probation_enabled: boolean;
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

export type Publication = "public" | "private";

/** A polled radar as its owner sees it, under `polled` in their node list. */
export interface OwnedPolledRadar {
  /** As the owner gave it, less any password. */
  address: string;
  unprotected: boolean;
  liveness: PolledRadarLiveness;
  trust_state: PolledRadarTrust;
}

/** One node in the owner's list, /api/auth/me/nodes. */
export interface OwnedNode {
  node_id: string;
  node_ref: string | null;
  name: string | null;
  status: string;
  last_heartbeat: string | null;
  is_synthetic: boolean;
  rx_lat: number | null;
  rx_lon: number | null;
  position_status: PositionStatus;
  frequency: number | null;
  location_private: boolean;
  /** Null for a node nobody claimed by email, which is every node an
   *  administrator assigned and every polled radar. */
  claimed_with: string | null;
  /** Null for every node but a polled radar. */
  polled: OwnedPolledRadar | null;
}
