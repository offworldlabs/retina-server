/**
 * What a downloaded file says about itself. The download route rebuilds the
 * archived Parquet as the legacy per-frame JSON, one object per frame with
 * the detections as parallel arrays and the geometry repeated on each, and
 * this reduces that to the handful of figures the drawer shows. Everything is
 * read defensively: the shape is whatever the archive held at the time.
 */

/** Frames tabulated in the drawer. */
export const PREVIEW_ROWS = 5;

export interface PreviewRow {
  timestampMs: number | null;
  /** The first detection's figures, or null for a frame with none. */
  delay: number | null;
  doppler: number | null;
  snr: number | null;
}

export interface Position {
  lat: number | null;
  lon: number | null;
  altFt: number | null;
}

export interface PreviewSummary {
  reportedNode: string | null;
  /** From the frames' own timestamps, so it can disagree with the hour the
   *  listing implies; null when no frame carries a usable one. */
  spanStartMs: number | null;
  spanEndMs: number | null;
  frames: number;
  detections: number;
  /** Detection slots carrying an ADS-B match, or null when no frame has the
   *  column at all. */
  adsb: { slots: number; hits: number } | null;
  signingMode: string | null;
  /** Frames that record a signing mode, and how many frames verified. Kept
   *  apart so the drawer never reports one frame's mode with another's
   *  verdict. */
  framesSigned: number;
  signaturesValid: number;
  /** The receiver as published, which is the fuzzed position. */
  rx: Position;
  /** The transmitter, which is true: they are licensed towers. */
  tx: Position;
  fcHz: number | null;
  fsHz: number | null;
  rows: PreviewRow[];
}

const num = (v: unknown): number | null => (Number.isFinite(v) ? (v as number) : null);

/** The first element of a detection array, or null when the frame has no
 *  detections or the column is missing. */
const first = (v: unknown): number | null => (Array.isArray(v) && v.length ? num(v[0]) : null);

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;

/** Null for a file that decoded to no frames, which the drawer reports as
 *  such rather than as a summary of nothing. */
export function summarisePreview(data: unknown): PreviewSummary | null {
  if (!isRecord(data) || !Array.isArray(data.detections)) return null;
  const frames = data.detections.filter(isRecord);
  if (!frames.length) return null;

  // A running min and max rather than a spread into Math.min: a busy hour
  // holds more frames than an argument list may carry.
  let spanStartMs: number | null = null;
  let spanEndMs: number | null = null;
  let detections = 0;
  let adsb: PreviewSummary["adsb"] = null;
  let signingMode: string | null = null;
  let framesSigned = 0;
  let signaturesValid = 0;
  for (const f of frames) {
    const t = num(f.timestamp);
    if (t !== null) {
      spanStartMs = spanStartMs === null ? t : Math.min(spanStartMs, t);
      spanEndMs = spanEndMs === null ? t : Math.max(spanEndMs, t);
    }
    // A verdict counts only on a frame that names its mode, so the valid
    // count can never exceed the signed count whatever a file holds.
    if (typeof f._signing_mode === "string" && f._signing_mode) {
      signingMode ??= f._signing_mode;
      framesSigned += 1;
      if (f._signature_valid === true) signaturesValid += 1;
    }
    if (Array.isArray(f.delay)) detections += f.delay.length;
    if (!Array.isArray(f.adsb)) continue;
    adsb ??= { slots: 0, hits: 0 };
    adsb.slots += f.adsb.length;
    adsb.hits += f.adsb.filter(Boolean).length;
  }

  // The geometry is the same on every frame that has it, so any one will do;
  // the first is taken in case an early frame was written without.
  const placed = frames.find((f) => num(f.rx_lat) !== null) ?? frames[0];

  return {
    reportedNode: typeof data.node_ref === "string" && data.node_ref ? data.node_ref : null,
    spanStartMs,
    spanEndMs,
    frames: frames.length,
    detections,
    adsb,
    signingMode,
    framesSigned,
    signaturesValid,
    rx: { lat: num(placed.rx_lat), lon: num(placed.rx_lon), altFt: num(placed.rx_alt_ft) },
    tx: { lat: num(placed.tx_lat), lon: num(placed.tx_lon), altFt: num(placed.tx_alt_ft) },
    fcHz: num(placed.fc_hz),
    fsHz: num(placed.fs_hz),
    rows: frames.slice(0, PREVIEW_ROWS).map((f) => {
      // No detection means no doppler or snr either, whatever those arrays hold.
      const delay = first(f.delay);
      return {
        timestampMs: num(f.timestamp),
        delay,
        doppler: delay === null ? null : first(f.doppler),
        snr: delay === null ? null : first(f.snr),
      };
    }),
  };
}

/** A UTC stamp to the second, compact enough for a table cell. */
export function isoUTC(ms: number): string {
  return `${new Date(ms).toISOString().replace("T", " ").slice(0, 19)}Z`;
}
