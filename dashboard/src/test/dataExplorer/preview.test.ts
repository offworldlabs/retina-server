import { describe, expect, it } from "vitest";

import { isoUTC, PREVIEW_ROWS, summarisePreview } from "../../pages/user/dataExplorer/preview";

const T0 = Date.parse("2026-09-17T05:00:00Z");

/** A frame as the download route rebuilds it: per-frame arrays of detections,
 *  with the geometry repeated on every frame. */
const frame = (offsetS: number, over: Record<string, unknown> = {}) => ({
  timestamp: T0 + offsetS * 1000,
  delay: [12.345, 20.1],
  doppler: [-30.5, 12],
  snr: [14.25, 9],
  adsb: [{ hex: "abc123" }, null],
  rx_lat: 51.5,
  rx_lon: -0.1,
  rx_alt_ft: 100,
  tx_lat: 51.6,
  tx_lon: -0.2,
  tx_alt_ft: 900,
  fc_hz: 204_640_000,
  fs_hz: 2_000_000,
  _signing_mode: "ed25519",
  _signature_valid: true,
  ...over,
});

describe("summarisePreview", () => {
  it("reads the span, the counts and the geometry off the frames", () => {
    const summary = summarisePreview({
      node_ref: "ret-a",
      detections: [frame(0), frame(60), frame(120)],
    });
    expect(summary).toMatchObject({
      reportedNode: "ret-a",
      spanStartMs: T0,
      spanEndMs: T0 + 120_000,
      frames: 3,
      detections: 6,
      adsb: { slots: 6, hits: 3 },
      signingMode: "ed25519",
      framesSigned: 3,
      signaturesValid: 3,
      rx: { lat: 51.5, lon: -0.1, altFt: 100 },
      tx: { lat: 51.6, lon: -0.2, altFt: 900 },
      fcHz: 204_640_000,
      fsHz: 2_000_000,
    });
  });

  it("is null for a file that decoded to no frames", () => {
    expect(summarisePreview({ node_ref: "ret-a", detections: [] })).toBeNull();
    expect(summarisePreview({ node_ref: "ret-a" })).toBeNull();
    expect(summarisePreview(null)).toBeNull();
    expect(summarisePreview("nope")).toBeNull();
  });

  it("tabulates the first detection of the first few frames", () => {
    const frames = Array.from({ length: PREVIEW_ROWS + 2 }, (_, i) => frame(i * 60));
    const summary = summarisePreview({ detections: frames });
    expect(summary.rows).toHaveLength(PREVIEW_ROWS);
    expect(summary.rows[0]).toEqual({ timestampMs: T0, delay: 12.345, doppler: -30.5, snr: 14.25 });
  });

  it("marks a frame with no detections rather than inventing a row", () => {
    const summary = summarisePreview({ detections: [frame(0, { delay: [], doppler: [], snr: [] })] });
    expect(summary.rows[0]).toEqual({ timestampMs: T0, delay: null, doppler: null, snr: null });
    expect(summary.detections).toBe(0);
  });

  it("reports no ADS-B column when no frame carries one", () => {
    const summary = summarisePreview({ detections: [frame(0, { adsb: undefined })] });
    expect(summary.adsb).toBeNull();
  });

  it("takes the geometry from the first frame that has a position", () => {
    const summary = summarisePreview({
      detections: [frame(0, { rx_lat: null, rx_lon: null }), frame(60, { rx_lat: 40, rx_lon: -3 })],
    });
    expect(summary.rx).toEqual({ lat: 40, lon: -3, altFt: 100 });
  });

  it("leaves the span empty when no frame has a usable timestamp", () => {
    const summary = summarisePreview({ detections: [frame(0, { timestamp: "soon" })] });
    expect(summary.spanStartMs).toBeNull();
    expect(summary.spanEndMs).toBeNull();
    expect(summary.rows[0].timestampMs).toBeNull();
  });

  it("records signing only when a frame says so", () => {
    const summary = summarisePreview({
      detections: [frame(0, { _signing_mode: undefined, _signature_valid: undefined })],
    });
    expect(summary.signingMode).toBeNull();
    expect(summary.framesSigned).toBe(0);
    expect(summary.signaturesValid).toBe(0);
  });

  it("counts the signatures that verified against the frames that were signed", () => {
    // One frame's signature failed: the verdict must not come from another
    // frame than the mode did, so both are counts rather than a single flag.
    const summary = summarisePreview({
      detections: [frame(0), frame(60, { _signature_valid: false }), frame(120)],
    });
    expect(summary.signingMode).toBe("ed25519");
    expect(summary.framesSigned).toBe(3);
    expect(summary.signaturesValid).toBe(2);
  });

  it("ignores a verdict on a frame that names no signing mode", () => {
    const summary = summarisePreview({
      detections: [frame(0, { _signature_valid: false }), frame(60, { _signing_mode: undefined })],
    });
    expect(summary.framesSigned).toBe(1);
    expect(summary.signaturesValid).toBe(0);
  });
});

describe("isoUTC", () => {
  it("is a compact UTC stamp to the second", () => {
    expect(isoUTC(Date.parse("2026-09-17T05:06:07.890Z"))).toBe("2026-09-17 05:06:07Z");
  });
});
