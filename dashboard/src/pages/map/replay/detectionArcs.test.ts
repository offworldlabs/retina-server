import { describe, expect, it } from "vitest";

import { buildBistaticArc, type NodeGeometry } from "../bistaticArc";
import { DETECTIONS, DETECTION_NODES } from "./__fixtures__/detections";
import { KEYED } from "./__fixtures__/scenarios";
import { buildDetectionWindow, detectionArcs, passiveHex } from "./detectionArcs";
import { buildReplayIndex, synthesizeFrame } from "./synthesize";
import type { DetectionRow, DetectionsResponse } from "./types";

/** A frame inside both fixtures: 2000z-known.json's tracks and 2000z-detections.json's rows. */
const CLOCK = Date.UTC(2026, 9, 18, 19, 57, 10);
/** The oldest instant whose arc still shows at CLOCK. */
const LO = CLOCK - 5_000;

/** The multinode hex of track A1 (key mn-adsb-a1dda8). */
const A1_HEX = "mne2f54ace80";
const PASSIVE: Record<string, string> = {
  nde0rho4ug5mfn9: "pr6b6740e72a",
  nde565zm6h3qldx: "prfd51945e73",
  ndekrn63vdrrp2f: "prd90c2908de",
  ndel9ksq8re9hlg: "pr019375a195",
  ndepfa2t1edmlce: "pr516feadec0",
  ndeysv8z5xpyd7f: "pr345f55a53c",
};

function arcsAtClock(
  resp: DetectionsResponse = DETECTIONS.tagged,
  nodes: Record<string, NodeGeometry> | null = DETECTION_NODES,
) {
  const window = buildDetectionWindow(resp, resp.from, resp.to);
  const frame = synthesizeFrame(buildReplayIndex(KEYED.known), CLOCK);
  const arcs = detectionArcs([{ window, lo: LO }], CLOCK, frame.aircraft, nodes);
  return { frame, arcs };
}

/** The fixture's rows a frame at CLOCK shows. */
const shown = DETECTIONS.tagged.rows.filter((r) => r.frame_ts_ms > LO && r.frame_ts_ms <= CLOCK);
const byTag = (hex: string | null) => shown.filter((r) => r.adsb_hex === hex);

/** The arc as built from the row's own receiver and transmitter, with the registry's beam and range. */
const rowArc = (r: DetectionRow, node: NodeGeometry) =>
  buildBistaticArc(r.delay_us!, {
    rx_lat: r.rx_lat!,
    rx_lon: r.rx_lon!,
    tx_lat: r.tx_lat!,
    tx_lon: r.tx_lon!,
    beam_azimuth_deg: node.beam_azimuth_deg,
    beam_width_deg: node.beam_width_deg,
    max_range_km: node.max_range_km,
    max_bistatic_range_km: node.max_bistatic_range_km,
  });

describe("detection arcs of a replayed frame", () => {
  it("draws each detection of (clock - 5 s, clock] once, stamped with its frame time", () => {
    expect(shown).toHaveLength(22);
    const { arcs } = arcsAtClock();
    expect(arcs).toHaveLength(22);
    expect(arcs.map((a) => [a.node_ref, a.ts, a.delay_us, a.doppler_hz])).toEqual(
      shown.map((r) => [r.node_ref, r.frame_ts_ms, r.delay_us, r.doppler_hz]),
    );
    expect(arcs[0].ts).toBe(Date.UTC(2026, 9, 18, 19, 57, 5, 969));
  });

  it("gives a detection tagged with a replayed track's aircraft that track's hex and altitude", () => {
    const { frame, arcs } = arcsAtClock();
    const a1 = frame.aircraft.find((ac) => ac.adsb_hex === "a1dda8");
    expect(a1).toMatchObject({ hex: A1_HEX, alt_baro: 35_030 });
    expect(byTag("a1dda8")).toHaveLength(4);
    const tagged = arcs.filter((a) => a.hex === A1_HEX);
    expect(tagged.map((a) => a.ts)).toEqual(byTag("a1dda8").map((r) => r.frame_ts_ms));
    expect(tagged.every((a) => a.alt_baro === 35_030)).toBe(true);
    expect(arcs.some((a) => a.hex === "a1dda8")).toBe(false);
  });

  it("keeps the hex of a tagged detection whose aircraft no replayed track holds, and adds no aircraft", () => {
    const { frame, arcs } = arcsAtClock();
    expect(byTag("a7a48e")).toHaveLength(3);
    const own = arcs.filter((a) => a.hex === "a7a48e");
    expect(own.map((a) => [a.node_ref, a.ts])).toEqual(byTag("a7a48e").map((r) => [r.node_ref, r.frame_ts_ms]));
    expect(own.every((a) => a.alt_baro === null)).toBe(true);
    // The private node's track is not in the tracks store, and the arcs do not put it there.
    expect(frame.aircraft).toEqual(synthesizeFrame(buildReplayIndex(KEYED.known), CLOCK).aircraft);
    expect(frame.aircraft.map((ac) => ac.adsb_hex ?? null).sort()).toEqual(["a1dda8", null]);
    expect(frame.detecting_nodes).toEqual({});
  });

  it("gives an untagged detection its node's passive hex", () => {
    const { arcs } = arcsAtClock();
    expect(byTag(null)).toHaveLength(15);
    const untagged = arcs.filter((a) => a.hex.startsWith("pr"));
    expect(untagged.map((a) => [a.node_ref, a.hex, a.alt_baro])).toEqual(
      byTag(null).map((r) => [r.node_ref, PASSIVE[r.node_ref], null]),
    );
    expect(passiveHex("nde565zm6h3qldx")).toBe("prfd51945e73");
  });

  it("builds each arc from the row's receiver and transmitter, with only the beam and range from the registry", () => {
    const { arcs } = arcsAtClock();
    arcs.forEach((arc, i) => {
      const row = shown[i];
      const node = DETECTION_NODES[row.node_ref];
      expect(arc.ambiguity_arc).toEqual(rowArc(row, node));
      // The registry's receiver is where the node is now (here an owner's true one), not where it was published.
      expect(arc.ambiguity_arc).not.toEqual(buildBistaticArc(row.delay_us!, node));
    });
    // A node the registry does not list gets the builder's own defaults (a 42-degree, 50 km area).
    const bare = arcsAtClock(DETECTIONS.tagged, {}).arcs;
    const expected = shown
      .map((r) => buildBistaticArc(r.delay_us!, { rx_lat: r.rx_lat!, rx_lon: r.rx_lon!, tx_lat: r.tx_lat!, tx_lon: r.tx_lon! }))
      .filter((arc) => arc !== null);
    expect(bare.map((a) => a.ambiguity_arc)).toEqual(expected);
    expect(bare.length).toBeLessThan(22);
  });

  it("draws nothing for a row without a delay above 0, a coordinate or a locus", () => {
    const [noDelay, zero, noRx, tooFar, ...rest] = shown;
    const rows = DETECTIONS.tagged.rows.map((r) => {
      if (r === noDelay) return { ...r, delay_us: null };
      if (r === zero) return { ...r, delay_us: 0 };
      if (r === noRx) return { ...r, rx_lat: null };
      // 1,500 km of differential range: beyond every bearing of a 170 km area.
      if (r === tooFar) return { ...r, delay_us: 5_000 };
      return r;
    });
    const { arcs } = arcsAtClock({ ...DETECTIONS.tagged, rows });
    expect(arcs.map((a) => a.ts)).toEqual(rest.map((r) => r.frame_ts_ms));
  });
});

describe("a detections window", () => {
  it("serves an answer the route cut up to its last row's frame time, and holds none of the rows at it", () => {
    const resp = DETECTIONS.truncated;
    const last = resp.rows[resp.rows.length - 1].frame_ts_ms;
    expect(last).toBe(Date.UTC(2026, 9, 18, 19, 48, 50, 632));
    const window = buildDetectionWindow(resp, resp.from, resp.to);
    expect(window).toMatchObject({ from: resp.from, to: resp.to, servedEnd: last, truncated: true, rowCount: 97 });
    expect(window.frameTs[96]).toBeLessThan(last);
  });

  it("serves a whole answer to its own end", () => {
    const resp = DETECTIONS.tagged;
    expect(buildDetectionWindow(resp, resp.from, resp.to)).toMatchObject({ servedEnd: resp.to, rowCount: 86, local: true });
    expect(buildDetectionWindow(DETECTIONS.notLocal, resp.from, resp.to).local).toBe(false);
  });
});
