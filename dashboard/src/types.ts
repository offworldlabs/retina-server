/* ------------------------------------------------------------------ */
/*  Shared API response types — RETINA dashboard                      */
/* ------------------------------------------------------------------ */

/** Which ends of a node's bistatic pair have coordinates. Orthogonal to a
 *  node's `status` (liveness): a node can be actively detecting and still
 *  be anything but "positioned". */
export type PositionStatus = "positioned" | "missing_rx" | "missing_tx" | "missing_both";

/* ---- Node location privacy ---- */

/** Where a node's effective location privacy came from: nothing set at all
 *  ("default", meaning public), the answer given at board onboarding
 *  ("registration"), or a dashboard override, which outranks both. */
export type LocationPrivacySource = "default" | "registration" | "override";

/** Reply shape of the owner and admin location-privacy writes, and of the
 *  admin read (which adds the raw pieces the effective answer came from). */
export interface LocationPrivacyState {
  node_id: string;
  location_private: boolean;
  location_privacy_source: LocationPrivacySource;
  /** Admin read only: the choice recorded at registration, if the node ever
   *  registered through the v1 API. */
  registration_choice?: "public" | "private" | null;
  /** Admin read only: the override row behind a source of "override". */
  override?: { private: boolean; set_by: string; set_at: number } | null;
  /** Convenience mirror of override.set_at for the control's source line. */
  set_at?: number | null;
}

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
