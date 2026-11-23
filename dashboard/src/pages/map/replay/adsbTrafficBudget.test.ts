// @vitest-environment node
/* ------------------------------------------------------------------ */
/*  The client budget with the ADS-B layer on (spec section 7: a      */
/*  replay tick under 8 ms): the 500-track window, a held detections  */
/*  window at the route's 100,000-row cap, and a held ADS-B hour at   */
/*  the seeded day's density (about 150 aircraft a snapshot). A tick  */
/*  at rate r synthesises the frame, the arcs of the TICK_MS * r of   */
/*  detections it newly shows, and the ground truth of its instant.   */
/*                                                                    */
/*  Prints what it measures and asserts the spec's tick budget at     */
/*  every rate, for the ground truth alone and for the whole tick     */
/*  (p95 under 8 ms). A tick's cost is the fastest of three runs of   */
/*  it, each from the trail cache as the tick before left it, so a    */
/*  collection or another process that stalls one is not taken for   */
/*  the tick's own work; the p95 and max of the first runs, the times */
/*  as played, are printed beside it. The heap figures need a forced  */
/*  GC (--expose-gc); without it they are not measured. Run:          */
/*    npx vitest run src/pages/map/replay/adsbTrafficBudget.test.ts   */
/*      --execArgv=--expose-gc --reporter=default                     */
/* ------------------------------------------------------------------ */

import { describe, expect, it } from "vitest";

import { ADSB } from "./__fixtures__/adsb";
import { DETECTION_NODES } from "./__fixtures__/detections";
import { ADSB_WINDOW_FROM, ADSB_WINDOW_TO, GENERATED_AIRCRAFT, generateAdsb } from "./__fixtures__/generateAdsb";
import { generateDetections } from "./__fixtures__/generateDetections";
import { BENCH_INSTANT_MS } from "./__fixtures__/scenarios";
import { benchClocks, generate500 } from "./__fixtures__/generate500";
import { adsbTraffic, buildAdsbWindow, type AdsbWindow, type TrafficCache } from "./adsbTraffic";
import { buildDetectionWindow, detectionArcs, type DetectionWindow } from "./detectionArcs";
import { buildReplayIndex, synthesizeFrame, type ReplayIndex } from "./synthesize";
import type { AdsbResponse } from "./types";
import { TICK_MS } from "./useArchiveReplay";

// Node's globals, typed here: the dashboard carries no Node type definitions.
const node = globalThis as unknown as {
  gc?: () => void;
  process: { memoryUsage(): { heapUsed: number; arrayBuffers: number } };
};
const MB = 1024 * 1024;

interface Reading {
  heap: number;
  buffers: number;
}

function reading(): Reading {
  node.gc?.();
  node.gc?.();
  const m = node.process.memoryUsage();
  return { heap: m.heapUsed, buffers: m.arrayBuffers };
}

const mbSince = (now: Reading, base: Reading) => (now.heap - base.heap + now.buffers - base.buffers) / MB;
const percentile = (sorted: number[], p: number) => sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)];
const sortedOf = (xs: number[]) => xs.slice().sort((a, b) => a - b);

// Each stage in a function of its own, so nothing it lets go of stays
// reachable from a stale register and skews the reading after it.

function fetchAdsb(): { resp: AdsbResponse; textMb: number } {
  const text = JSON.stringify(generateAdsb());
  return { resp: JSON.parse(text), textMb: text.length / MB };
}

function loadAdsb() {
  const { resp, textMb } = fetchAdsb();
  const parsed = reading();
  const t0 = performance.now();
  const window = buildAdsbWindow(resp, resp.from, resp.to);
  const buildMs = performance.now() - t0;
  return { window, buildMs, textMb, parsed };
}

function loadDetections(): DetectionWindow {
  const hexes = [...new Set(generate500().rows.flatMap((r) => (r.adsb_hex ? [r.adsb_hex] : [])))].sort();
  const resp = generateDetections(hexes);
  return buildDetectionWindow(resp, resp.from, resp.to);
}

interface TickFigures {
  rate: number;
  ticks: number;
  aircraftPerTick: number;
  arcsPerTick: number;
  truthP95: number;
  truthMax: number;
  p95: number;
  max: number;
  median: number;
  playedP95: number;
  playedMax: number;
}

/**
 * Plays `ticks` ticks at `rate` from BENCH_INSTANT_MS: each synthesises the
 * 500-track frame at a bench clock, the arcs of the next TICK_MS * rate of
 * detections, and the ground truth of the ADS-B clock, which moves on by
 * TICK_MS * rate a tick, so a new snapshot is read every 10 s of replay as
 * playback reads it. Every tick runs three times from the trail cache the
 * tick before left, and costs the fastest of the three.
 */
function playTicks(index: ReplayIndex, detections: DetectionWindow, adsb: AdsbWindow, rate: number, ticks: number): TickFigures {
  const clocks = benchClocks();
  const step = TICK_MS * rate;
  const cache: TrafficCache = new Map();
  const truths: number[] = [];
  const totals: number[] = [];
  const played: number[] = [];
  let aircraft = 0;
  let arcs = 0;
  for (let k = 0; k < ticks; k++) {
    const hi = detections.from + (k + 1) * step;
    const clock = BENCH_INSTANT_MS + k * step;
    const before = new Map(cache);
    let total = Infinity;
    let truth = Infinity;
    for (let run = 0; run < 3; run++) {
      const runCache = run === 0 ? cache : new Map(before);
      const t0 = performance.now();
      const frame = synthesizeFrame(index, clocks[k % clocks.length]);
      const drawnArcs = detectionArcs([{ window: detections, lo: hi - step }], hi, frame.aircraft, DETECTION_NODES).length;
      const t1 = performance.now();
      const shown = adsbTraffic([adsb], clock, runCache);
      const t2 = performance.now();
      if (run === 0) {
        aircraft += Object.keys(shown.ground_truth).length;
        arcs += drawnArcs;
        played.push(t2 - t0);
      }
      total = Math.min(total, t2 - t0);
      truth = Math.min(truth, t2 - t1);
    }
    totals.push(total);
    truths.push(truth);
  }
  const t = sortedOf(truths);
  const all = sortedOf(totals);
  const p = sortedOf(played);
  return {
    rate,
    ticks,
    aircraftPerTick: aircraft / ticks,
    arcsPerTick: arcs / ticks,
    truthP95: percentile(t, 95),
    truthMax: t[t.length - 1],
    p95: percentile(all, 95),
    max: all[all.length - 1],
    median: percentile(all, 50),
    playedP95: percentile(p, 95),
    playedMax: p[p.length - 1],
  };
}

describe("a realistic ADS-B hour with every layer on", () => {
  it("has the fields and value types of the route's rows, at about 150 aircraft a snapshot", () => {
    const generated = generateAdsb();
    const real = ADSB.traffic.rows;
    const shape = (r: object) => JSON.stringify(Object.entries(r).map(([k, v]) => [k, v === null ? "null" : typeof v]));
    expect([...new Set(generated.rows.map(shape))].sort()).toEqual([...new Set(real.map(shape))].sort());
    expect(generated.rows.every((r, i) => i === 0 || generated.rows[i - 1].ts_ms <= r.ts_ms)).toBe(true);
    expect(generated.rows.every((r) => r.ts_ms >= ADSB_WINDOW_FROM && r.ts_ms < ADSB_WINDOW_TO)).toBe(true);
    const perSnapshot = new Map<number, number>();
    for (const r of generated.rows) perSnapshot.set(r.ts_ms, (perSnapshot.get(r.ts_ms) ?? 0) + 1);
    expect(perSnapshot.size).toBe(360);
    const counts = sortedOf([...perSnapshot.values()]);
    expect(percentile(counts, 50)).toBeGreaterThan(130);
    expect(percentile(counts, 50)).toBeLessThan(170);
    expect(generated.rows.length).toBeGreaterThan(50_000);
    expect(generated.rows.length).toBeLessThan(58_000);
    expect(new Set(generated.rows.map((r) => r.hex)).size).toBeLessThanOrEqual(GENERATED_AIRCRAFT);
    expect(generateAdsb()).toEqual(generated); // deterministic
  });

  it("holds the window, and the ground truth and the whole tick stay within budget", { timeout: 180_000 }, () => {
    const index = buildReplayIndex(generate500());
    const detections = loadDetections();
    const base = reading();
    const { window, buildMs, textMb, parsed } = loadAdsb();
    const held = reading();

    const rates = [
      playTicks(index, detections, window, 1, 200),
      playTicks(index, detections, window, 5, 200),
      playTicks(index, detections, window, 20, 120),
    ];

    // A frame after a seek builds every trail it hands over, with nothing to reuse.
    const cold: number[] = [];
    let coldAircraft = 0;
    for (let k = 0; k < 50; k++) {
      const t0 = performance.now();
      coldAircraft = Object.keys(adsbTraffic([window], BENCH_INSTANT_MS + k * 7_000, new Map()).ground_truth).length;
      cold.push(performance.now() - t0);
    }
    const coldSorted = sortedOf(cold);

    const ms = (x: number) => x.toFixed(3);
    const lines = [
      `adsb budget: ${window.rowCount} rows over 60 min (${window.hexes.length} aircraft), response JSON ${textMb.toFixed(1)} MB, ` +
        `window build ${buildMs.toFixed(1)} ms; 500 tracks visible in every frame, arcs from 100,000 detections`,
      ...rates.map(
        (r) =>
          `  ${r.rate}x over ${r.ticks} ticks, ${r.aircraftPerTick.toFixed(1)} aircraft and ${r.arcsPerTick.toFixed(1)} arcs a frame: ` +
          `ground truth p95 ${ms(r.truthP95)} ms, ` +
          `max ${ms(r.truthMax)} ms; whole tick median ${ms(r.median)} ms, p95 ${ms(r.p95)} ms, max ${ms(r.max)} ms; ` +
          `as played p95 ${ms(r.playedP95)} ms, max ${ms(r.playedMax)} ms`,
      ),
      `  ground truth after a seek (${coldAircraft} trails built, no cache), 50 instants: ` +
        `median ${ms(percentile(coldSorted, 50))} ms, p95 ${ms(percentile(coldSorted, 95))} ms, max ${ms(coldSorted[coldSorted.length - 1])} ms`,
    ];
    if (node.gc) {
      lines.push(
        `  parsed response ${mbSince(parsed, base).toFixed(1)} MB; window held ${mbSince(held, base).toFixed(1)} MB ` +
          `(heap ${((held.heap - base.heap) / MB).toFixed(1)} + array buffers ${((held.buffers - base.buffers) / MB).toFixed(1)})`,
      );
    } else {
      lines.push("  heap not measured: run with --execArgv=--expose-gc");
    }
    console.info(lines.join("\n"));

    for (const r of rates) {
      expect.soft(r.aircraftPerTick, `${r.rate}x aircraft per frame`).toBeGreaterThan(130);
      expect.soft(r.arcsPerTick, `${r.rate}x arcs per frame`).toBeGreaterThan(40 * r.rate);
      expect.soft(r.truthP95, `${r.rate}x ground truth p95 (ms)`).toBeLessThan(8);
      expect.soft(r.p95, `${r.rate}x whole tick p95 (ms)`).toBeLessThan(8);
    }
    expect.soft(percentile(coldSorted, 95), "ground truth after a seek p95 (ms)").toBeLessThan(8);
    if (node.gc) expect(mbSince(held, base)).toBeLessThan(50);
  });
});
