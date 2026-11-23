import { api } from "../../../api/client";

/**
 * The public feeds behind RF Environment's network view, /api/radar/nodes and
 * /api/radar/analytics, read into one list. Both are keyed on node_ref, the
 * only identity they carry, and neither lists a private node: the listing
 * leaves it out, so the join below does too.
 */

/** A node's geometry as the public listing publishes it: the receiver moved
 *  by the network's location fuzz, the transmitter as it stands. */
export interface PublicLocation {
  rx_lat?: number | null;
  rx_lon?: number | null;
  rx_alt_ft?: number | null;
  tx_lat?: number | null;
  tx_lon?: number | null;
  tx_alt_ft?: number | null;
  /** How far the published receiver may be from the true one; 0 with the fuzz off. */
  location_uncertainty_km?: number | null;
}

/** One entry of /api/radar/nodes, the keys this page reads. */
interface PublicNode {
  name?: string | null;
  /** Carrier, Hz. */
  frequency?: number | null;
  /** Hz. */
  sample_rate?: number | null;
  location?: PublicLocation | null;
}

/** The blocks of an analytics summary this page reads, the public one or the
 *  owner's. Read as unknown and checked where read: either block can be
 *  missing, and a figure the summary has not computed yet can be null. */
export interface AnalyticsSummary {
  metrics?: { avg_snr?: unknown; total_frames?: unknown; total_detections?: unknown } | null;
  detection_area?: { n_detections?: unknown; center_freq?: unknown } | null;
}

interface PublicListing {
  nodes?: Record<string, PublicNode> | null;
}

interface PublicAnalytics {
  nodes?: Record<string, AnalyticsSummary> | null;
}

/** One node of the public listing with its analytics beside it. */
export interface NetworkNode {
  ref: string;
  name: string | null;
  frequency: number | null;
  sampleRate: number | null;
  location: PublicLocation;
  /** Null for a node the analytics have not reached yet. */
  summary: AnalyticsSummary | null;
}

export interface NetworkSnapshot {
  nodes: NetworkNode[];
  /** When the answer landed, epoch ms: the time its samples are taken at. */
  at: number;
}

/** A number the payload carried as one, else null: never NaN, never a string. */
export function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A node's average SNR as its summary gives it, dB. */
export function averageSnr(summary: AnalyticsSummary | null): number | null {
  return finite(summary?.metrics?.avg_snr);
}

function own<T>(map: Record<string, T>, key: string): T | null {
  // hasOwnProperty, not a plain lookup: the keys come from the server.
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : null;
}

export async function fetchNetwork(): Promise<NetworkSnapshot> {
  const [listing, analytics]: [PublicListing | null, PublicAnalytics | null] = await Promise.all([
    api.nodes(),
    api.analytics(),
  ]);
  const summaries = analytics?.nodes ?? {};
  const nodes = Object.entries(listing?.nodes ?? {}).map(([ref, node]): NetworkNode => ({
    ref,
    name: typeof node?.name === "string" && node.name !== "" ? node.name : null,
    frequency: finite(node?.frequency),
    sampleRate: finite(node?.sample_rate),
    location: node?.location ?? {},
    summary: own(summaries, ref),
  }));
  return { nodes, at: Date.now() };
}

/* ── The live trend ─────────────────────────────────────────────────────── */

/** One reading of a node's running average SNR, as the public analytics gave
 *  it at the time. Not a detection's SNR: the average over all of them. */
export interface SnrSample {
  /** When it was read, epoch ms. */
  t: number;
  /** dB. */
  snr: number;
}

/** Each network node's samples, oldest first, keyed on node_ref. */
export type SampleHistory = ReadonlyMap<string, SnrSample[]>;

export const NO_SAMPLES: SampleHistory = new Map();

/** How many samples a node keeps: the latest and the thirty before it. */
export const SAMPLE_CAP = 31;

/**
 * Every listed node's sample from one answer, appended to its history. Every
 * node is sampled, not only the one on screen, so the trend of any node the
 * viewer turns to runs from when the page opened. A node that reports no
 * average yet gets no sample rather than a zero, which would plot a reading
 * that was never taken.
 */
export function recordSamples(history: SampleHistory, snapshot: NetworkSnapshot): SampleHistory {
  const next = new Map(history);
  for (const node of snapshot.nodes) {
    const snr = averageSnr(node.summary);
    if (snr === null) continue;
    const earlier = history.get(node.ref) ?? [];
    next.set(node.ref, [...earlier.slice(-(SAMPLE_CAP - 1)), { t: snapshot.at, snr }]);
  }
  return next;
}
