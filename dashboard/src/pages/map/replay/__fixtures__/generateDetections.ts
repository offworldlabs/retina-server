/* ------------------------------------------------------------------ */
/*  The arcs layer's worst window: the detections route's 100,000-row */
/*  cap over its widest window, 10 minutes, around generate500's      */
/*  bench instant (2026-10-18 [14:10Z, 14:20Z)). Deterministic.       */
/*                                                                    */
/*  Shaped from 2000z-detections.json: its six nodes, each with the   */
/*  published receiver and transmitter its rows carry; delays and     */
/*  Dopplers drawn around that node's own rows, at the precision the  */
/*  route sends; a solve seen by two to five nodes at one frame time; */
/*  and about the fixture's share of untagged rows. Tagged rows name  */
/*  the aircraft the caller passes (the window's replayed tracks),    */
/*  and a few name one no replayed track holds.                       */
/* ------------------------------------------------------------------ */

import type { DetectionRow, DetectionsResponse } from "../types";
import { DETECTIONS } from "./detections";

export const GENERATED_DETECTIONS = 100_000;
export const DETECTIONS_WINDOW_FROM = Date.UTC(2026, 9, 18, 14, 10, 0);
export const DETECTIONS_WINDOW_TO = Date.UTC(2026, 9, 18, 14, 20, 0);

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const round = (x: number, d: number) => Math.round(x * 10 ** d) / 10 ** d;

/** 100,000 detections in the route's order; `hexes` are the ADS-B hexes tagged rows name. */
export function generateDetections(hexes: readonly string[], seed = 100): DetectionsResponse {
  const rand = mulberry32(seed);
  const pick = <T,>(xs: readonly T[]) => xs[Math.floor(rand() * xs.length)];

  const byNode = new Map<string, DetectionRow[]>();
  for (const row of DETECTIONS.tagged.rows) {
    const list = byNode.get(row.node_ref) ?? [];
    list.push(row);
    byNode.set(row.node_ref, list);
  }
  const nodes = [...byNode.keys()].sort();
  const untaggedShare = DETECTIONS.tagged.rows.filter((r) => r.adsb_hex === null).length / DETECTIONS.tagged.rows.length;

  const span = DETECTIONS_WINDOW_TO - DETECTIONS_WINDOW_FROM;
  const rows: DetectionRow[] = [];
  while (rows.length < GENERATED_DETECTIONS) {
    const ts = DETECTIONS_WINDOW_FROM + Math.floor(rand() * span);
    const seen = 2 + Math.floor(rand() * 4);
    const tagRoll = rand();
    const hex = tagRoll < untaggedShare ? null : tagRoll < 0.95 ? pick(hexes) : "a7a48e";
    const order = nodes.slice();
    for (let i = 0; i < seen; i++) {
      const j = i + Math.floor(rand() * (order.length - i));
      [order[i], order[j]] = [order[j], order[i]];
    }
    for (const ref of order.slice(0, seen)) {
      if (rows.length === GENERATED_DETECTIONS) break;
      const like = pick(byNode.get(ref)!);
      rows.push({
        node_ref: ref,
        frame_ts_ms: ts,
        delay_us: round(Math.max(1, like.delay_us! + (rand() - 0.5) * 40), 3),
        doppler_hz: round(like.doppler_hz! + (rand() - 0.5) * 40, 3),
        snr_db: round(6 + rand() * 18, 2),
        adsb_hex: hex,
        rx_lat: like.rx_lat,
        rx_lon: like.rx_lon,
        tx_lat: like.tx_lat,
        tx_lon: like.tx_lon,
      });
    }
  }
  rows.sort(
    (a, b) =>
      a.frame_ts_ms - b.frame_ts_ms ||
      (a.node_ref < b.node_ref ? -1 : a.node_ref > b.node_ref ? 1 : 0) ||
      a.delay_us! - b.delay_us!,
  );
  return {
    from: DETECTIONS_WINDOW_FROM,
    to: DETECTIONS_WINDOW_TO,
    fleet: "real",
    count: rows.length,
    truncated: false,
    local: true,
    pending: false,
    rows,
  };
}
