// @vitest-environment node
/* ------------------------------------------------------------------ */
/*  The client budget for the worst window (spec section 7: a replay  */
/*  tick under 8 ms and under 50 MB per loaded window, 500 tracks).    */
/*                                                                    */
/*  Prints what it measures and asserts only bounds far above it, so  */
/*  a slow or busy machine cannot fail it. The heap figures need a    */
/*  forced GC, which Node exposes only with --expose-gc; without it   */
/*  they are not measured and their assertions are skipped. Vitest's  */
/*  agent reporter hides a passing test's output, hence --reporter:   */
/*    npx vitest run src/pages/map/replay/budget.test.ts \            */
/*      --execArgv=--expose-gc --reporter=default                     */
/* ------------------------------------------------------------------ */

import { describe, expect, it } from "vitest";
import { groupTrackRows } from "./group";
import { buildReplayIndex, synthesizeFrame, type ReplayIndex } from "./synthesize";
import type { TrackRow, TracksResponse } from "./types";
import { GENERATED_ROWS_PER_TRACK, GENERATED_TRACKS, benchClocks, generate500 } from "./__fixtures__/generate500";
import { BENCH_INSTANT_MS, BENCH_SAMPLE, LEGACY } from "./__fixtures__/scenarios";

// Node's globals, typed here: the dashboard carries no Node type definitions.
const node = globalThis as unknown as {
  gc?: () => void;
  process: { memoryUsage(): { heapUsed: number; arrayBuffers: number } };
};
const MB = 1024 * 1024;

/** Heap in use plus ArrayBuffer backing stores, which live outside the heap and hold the index's columns. */
interface Reading {
  heap: number;
  buffers: number;
}

function reading(collect = true): Reading {
  if (collect) {
    node.gc?.();
    node.gc?.();
  }
  const m = node.process.memoryUsage();
  return { heap: m.heapUsed, buffers: m.arrayBuffers };
}

const mbSince = (now: Reading, base: Reading) => (now.heap - base.heap + now.buffers - base.buffers) / MB;

const percentile = (sorted: number[], p: number) => sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)];

/**
 * Each field's JSON type: null, boolean, string, an array's element type (an
 * empty one counts as strings), and for a number of a field in `integers`
 * whether it is an integer.
 */
function shapeOf(row: TrackRow, integers: ReadonlySet<string>): string {
  const type = (key: string, v: unknown): string => {
    if (v === null) return "null";
    if (Array.isArray(v)) return `${[...new Set(v.map((x) => typeof x))].sort().join("|") || "string"}[]`;
    if (typeof v === "number" && integers.has(key)) return Number.isInteger(v) ? "integer" : "fraction";
    return typeof v;
  };
  return JSON.stringify(Object.entries(row).map(([k, v]) => [k, type(k, v)]).sort(([a], [b]) => (a < b ? -1 : 1)));
}

// Each stage runs in a function of its own: a value a caller has let go of can
// otherwise stay reachable from a stale interpreter register and skew the
// reading after it.

/** The window as fetch().json() hands it over: its JSON text, parsed. */
function fetchWindow(): { resp: TracksResponse; textMb: number } {
  const text = JSON.stringify(generate500());
  return { resp: JSON.parse(text), textMb: text.length / MB };
}

/** Parses and indexes the window, then lets go of the response. */
function loadWindow() {
  const { resp, textMb } = fetchWindow();
  const parsed = reading();
  const t0 = performance.now();
  const index: ReplayIndex = buildReplayIndex(resp);
  const buildMs = performance.now() - t0;
  const unswept = reading(false);
  const both = reading();
  return { index, buildMs, textMb, parsed, unswept, both };
}

describe("the worst window", () => {
  it("holds 500 tracks in the API's 50,000-row cap, all visible at the bench instant with long trails", () => {
    const resp = generate500();
    expect(resp.rows).toHaveLength(GENERATED_TRACKS * GENERATED_ROWS_PER_TRACK);
    expect(resp.rows).toHaveLength(50_000);
    expect(resp.count).toBe(resp.rows.length);
    for (let i = 1; i < resp.rows.length; i++) {
      const a = resp.rows[i - 1];
      const b = resp.rows[i];
      expect(a.frame_ts_ms < b.frame_ts_ms || (a.frame_ts_ms === b.frame_ts_ms && a.solve_ts_ms <= b.solve_ts_ms)).toBe(true);
    }
    expect(resp.rows.every((r) => r.solve_ts_ms >= resp.from && r.solve_ts_ms < resp.to)).toBe(true);
    // The mix: keyed known, dark and follow tracks, legacy adsb rows, legacy dark rows to link.
    const kinds = new Set(resp.rows.map((r) => `${r.lane}:${r.track_key === null}:${r.adsb_hex === null}`));
    expect([...kinds].sort()).toEqual(["dark:false:true", "follow:false:true", "known:false:false", "null:true:false", "null:true:true"]);
    expect(groupTrackRows(resp.rows)).toHaveLength(GENERATED_TRACKS);

    const frame = synthesizeFrame(buildReplayIndex(resp), BENCH_INSTANT_MS);
    expect(frame.aircraft).toHaveLength(GENERATED_TRACKS);
    expect(Math.min(...frame.aircraft.map((a) => a.recent_positions.length))).toBeGreaterThanOrEqual(90);
    expect(generate500()).toEqual(resp); // deterministic
  });

  it("has the fields and value types of a real response's rows, schema by schema", () => {
    // Keyed rows against scenario c's real 500-track window, legacy ones against real legacy rows.
    const real = { keyed: BENCH_SAMPLE.rows, legacy: Object.values(LEGACY).flatMap((resp) => resp.rows) };
    const all = [...real.keyed, ...real.legacy];
    const fields = Object.keys(all[0]).sort();
    const integers = new Set(fields.filter((k) => all.every((r) => Number.isInteger(r[k as keyof TrackRow]))));
    expect([...integers]).toEqual(["frame_ts_ms", "n_nodes", "solve_ts_ms"]);

    const shapes = (rows: TrackRow[]) => [...new Set(rows.map((r) => shapeOf(r, integers)))].sort();
    const generated = generate500().rows;
    expect([...new Set(generated.map((r) => Object.keys(r).sort().join()))]).toEqual([fields.join()]);
    expect(shapes(generated.filter((r) => r.track_key !== null))).toEqual(shapes(real.keyed));
    expect(shapes(generated.filter((r) => r.track_key === null))).toEqual(shapes(real.legacy));
  });

  it("indexes once and ticks within budget", () => {
    const base = reading();
    const { index, buildMs, textMb, parsed, unswept, both } = loadWindow();
    const indexOnly = reading();

    const clocks = benchClocks();
    expect(clocks.length).toBeGreaterThanOrEqual(200);
    const ticks: number[] = [];
    let fewest = Infinity;
    for (const clock of clocks) {
      const t0 = performance.now();
      const frame = synthesizeFrame(index, clock);
      ticks.push(performance.now() - t0);
      fewest = Math.min(fewest, frame.aircraft.length);
    }
    expect(fewest).toBe(GENERATED_TRACKS);
    const sorted = ticks.slice().sort((a, b) => a - b);
    const median = percentile(sorted, 50);
    const p95 = percentile(sorted, 95);
    const max = sorted[sorted.length - 1];

    const ms = (x: number) => x.toFixed(3);
    const mb = (x: number) => `${x.toFixed(1)} MB`;
    const lines = [
      `replay budget: ${index.rowCount} rows, ${index.trackCount} tracks, response JSON ${mb(textMb)}`,
      `  index build ${buildMs.toFixed(1)} ms`,
      `  tick over ${ticks.length} clocks 250 ms apart, ${fewest} visible in each: ` +
        `median ${ms(median)} ms, p95 ${ms(p95)} ms, max ${ms(max)} ms (first ${ms(ticks[0])} ms)`,
    ];
    if (node.gc) {
      lines.push(
        `  parsed response ${mb(mbSince(parsed, base))}`,
        `  peak, response + index ${mb(mbSince(both, base))} (${mb(mbSince(unswept, base))} before the build's garbage is collected)`,
        `  index retained ${mb(mbSince(indexOnly, base))} ` +
          `(heap ${mb((indexOnly.heap - base.heap) / MB)} + array buffers ${mb((indexOnly.buffers - base.buffers) / MB)})`,
      );
    } else {
      lines.push("  heap not measured: run with --execArgv=--expose-gc");
    }
    console.info(lines.join("\n"));

    expect(median).toBeLessThan(50);
    expect(p95).toBeLessThan(100);
    expect(buildMs).toBeLessThan(10_000);
    if (node.gc) {
      expect(mbSince(indexOnly, base)).toBeLessThan(200);
      expect(mbSince(both, base)).toBeLessThan(400);
    }
  });
});
