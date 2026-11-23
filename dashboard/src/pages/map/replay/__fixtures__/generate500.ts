/* ------------------------------------------------------------------ */
/*  The client's worst window: 500 tracks and the API's 50,000-row    */
/*  cap, in a 2026-10-18 [14:00Z, 14:30Z) response. Deterministic.    */
/*                                                                    */
/*  Each track has 100 rows at a 1 s cadence. Starts are staggered    */
/*  over 10 s so that around BENCH_INSTANT_MS every track is visible  */
/*  with a long trail. The mix: known, dark and follow keyed tracks,  */
/*  legacy rows keyed only by adsb_hex, and legacy dark rows that     */
/*  must be linked (laid out over 10 km apart, flying in formation,   */
/*  so linking finds exactly one track per target).                   */
/* ------------------------------------------------------------------ */

import type { Lane, TrackRow, TracksResponse } from "../types";
import { BENCH_HALF_SPAN_MS, BENCH_INSTANT_MS } from "./scenarios";

export const GENERATED_TRACKS = 500;
export const GENERATED_ROWS_PER_TRACK = 100;

const WINDOW_FROM = Date.UTC(2026, 9, 18, 14, 0, 0);
const WINDOW_TO = Date.UTC(2026, 9, 18, 14, 30, 0);
const M_PER_DEG = 111_320;

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

type Kind = "known" | "dark" | "follow" | "legacy-adsb" | "legacy-dark";

/** Track i's kind: 200 known, 150 dark, 50 follow, 50 legacy adsb, 50 legacy dark. */
function kindOf(i: number): Kind {
  if (i < 200) return "known";
  if (i < 350) return "dark";
  if (i < 400) return "follow";
  if (i < 450) return "legacy-adsb";
  return "legacy-dark";
}

export function generate500(seed = 500): TracksResponse {
  const rand = mulberry32(seed);
  const refs: string[] = [];
  while (refs.length < 40) {
    let s = "nde";
    for (let i = 0; i < 10; i++) s += Math.floor(rand() * 16).toString(16);
    if (!refs.includes(s)) refs.push(s);
  }
  const hexes = new Set<string>();
  const newHex = () => {
    for (;;) {
      const h = Math.floor(rand() * 0xffffff).toString(16).padStart(6, "0");
      if (!hexes.has(h)) {
        hexes.add(h);
        return h;
      }
    }
  };

  // Every track's first frame lies in [bench - 102 s, bench - 92 s]: by the
  // bench instant each has published at least 90 rows, and its last solve is
  // still inside the dark expiry BENCH_HALF_SPAN_MS after it.
  const firstFrame0 = BENCH_INSTANT_MS - 102_000;
  const rows: TrackRow[] = [];

  for (let i = 0; i < GENERATED_TRACKS; i++) {
    const kind = kindOf(i);
    const legacy = kind === "legacy-adsb" || kind === "legacy-dark";
    let frame = firstFrame0 + i * 20;
    let lat: number;
    let lon: number;
    let heading: number;
    let speed: number;
    if (kind === "legacy-dark") {
      // A 10 x 5 grid 12 km apart, all on one heading at one speed.
      const j = i - 450;
      lat = 34.5 + (j % 10) * (12_000 / M_PER_DEG);
      lon = -83.3 + Math.floor(j / 10) * 0.14;
      heading = 90;
      speed = 150;
    } else {
      lat = 34.45 + rand() * 0.8;
      lon = -82.8 + rand() * 0.8;
      heading = rand() * 360;
      speed = 50 + rand() * 200;
    }
    let alt = 300 + rand() * 10_700;
    const climb = (rand() - 0.5) * 10;
    const adsbHex = kind === "known" || kind === "legacy-adsb" ? newHex() : null;
    let key: string | null = null;
    if (kind === "known") key = `mn-adsb-${adsbHex}`;
    if (kind === "dark" || kind === "follow") key = `mn-dark-${frame}-${lat.toFixed(3)}-${lon.toFixed(3)}`;
    const lane: Lane | null = legacy ? null : kind === "known" ? "known" : kind === "follow" ? "follow" : "dark";
    const sigmaLo = kind === "known" || kind === "legacy-adsb" ? 50 : 380;
    const sigmaHi = kind === "known" || kind === "legacy-adsb" ? 320 : 2000;
    const anomalousFrom = !legacy && rand() < 0.05 ? 20 + Math.floor(rand() * 40) : -1;
    let latency = 600 + Math.floor(rand() * 500);
    let nodeRefs: string[] = [];

    for (let k = 0; k < GENERATED_ROWS_PER_TRACK; k++) {
      if (k > 0) {
        const dt = 1000 + Math.floor(rand() * 30) - 15;
        frame += dt;
        const h = (heading * Math.PI) / 180;
        const d = (speed * dt) / 1000;
        lat += (d * Math.cos(h)) / M_PER_DEG;
        lon += (d * Math.sin(h)) / (M_PER_DEG * Math.cos((lat * Math.PI) / 180));
        alt += (climb * dt) / 1000;
      }
      if (k % 20 === 0) {
        const n = 2 + Math.floor(rand() * 4);
        const set = new Set<string>();
        while (set.size < n) set.add(refs[Math.floor(rand() * refs.length)]);
        nodeRefs = [...set];
      }
      latency = Math.min(1500, Math.max(300, latency + Math.floor(rand() * 301) - 150));
      const h = (heading * Math.PI) / 180;
      const noise = () => (rand() - 0.5) * 2;
      const anomalous = anomalousFrom >= 0 && k >= anomalousFrom && k < anomalousFrom + 15;
      rows.push({
        frame_ts_ms: frame,
        solve_ts_ms: frame + latency,
        track_key: key,
        lane,
        node_kind: "real",
        lat: round(lat + (noise() * 25) / M_PER_DEG, 7),
        lon: round(lon + (noise() * 25) / M_PER_DEG, 7),
        alt_m: round(alt + noise() * 15, 1),
        vel_east_ms: round(speed * Math.sin(h) + noise() * 2, 2),
        vel_north_ms: round(speed * Math.cos(h) + noise() * 2, 2),
        vel_up_ms: round(climb + noise() * 0.5, 2),
        n_nodes: nodeRefs.length,
        node_refs: nodeRefs,
        adsb_hex: adsbHex,
        pos_sigma_m: legacy ? null : round(sigmaLo + rand() * (sigmaHi - sigmaLo), 1),
        rms_delay_us: round(0.08 + rand() * 1.1, 4),
        rms_doppler_hz: round(0.4 + rand() * 5.5, 3),
        is_anomalous: legacy ? null : anomalous,
        anomaly_types: anomalous ? ["speed", "altitude"] : [],
      });
    }
  }

  rows.sort((a, b) => a.frame_ts_ms - b.frame_ts_ms || a.solve_ts_ms - b.solve_ts_ms);
  return { from: WINDOW_FROM, to: WINDOW_TO, fleet: "real", count: rows.length, truncated: false, rows };
}

/** The bench clocks: 250 ms apart across BENCH_INSTANT_MS +- BENCH_HALF_SPAN_MS. */
export function benchClocks(): number[] {
  const clocks: number[] = [];
  for (let c = BENCH_INSTANT_MS - BENCH_HALF_SPAN_MS; c < BENCH_INSTANT_MS + BENCH_HALF_SPAN_MS; c += 250) clocks.push(c);
  return clocks;
}
