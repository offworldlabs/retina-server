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

/** A stock blah2 radar the server polls, as administrators see it. */
export interface PolledRadar {
  node_id: string;
  node_ref: string;
  /** `host:port`: the operator's address, shown only to admins and the owner. */
  endpoint: string;
  liveness: "pending" | "streaming" | "stalled" | "unreachable";
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
