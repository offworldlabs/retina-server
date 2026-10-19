import { act, cleanup, renderHook } from "@testing-library/react";
import { HttpError, UnauthorizedError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../../api/client";
import { useAircraftFeed, type InjectedFrame } from "../hooks";
import { writeArchiveHash } from "../useUrlHashState";
import { KEYED } from "./__fixtures__/scenarios";
import { mintedDarkKey } from "./group";
import { multinodeHexFromKey } from "./hex";
import { buildReplayIndex, synthesizeFrame } from "./synthesize";
import type { TrackRow, TracksResponse } from "./types";
import {
  ARCHIVE_EARLIEST_MS,
  ARCHIVE_LAG_MS,
  FLUSH_PERIOD_MS,
  LIVE_EDGE_MS,
  WINDOW_LEAD_IN_MS,
  useArchiveReplay,
  type ArchiveReplay,
} from "./useArchiveReplay";

vi.mock("../../../api/client", () => ({ api: { archiveTracks: vi.fn(), trackAvailability: vi.fn() } }));
vi.mock("../useUrlHashState", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../useUrlHashState")>()),
  writeArchiveHash: vi.fn(),
}));
// For the feed hook in the last block.
vi.mock("../../../utils/domains", () => ({ usesRealOnlyFeed: false }));
vi.mock("../../../context/AuthContext", () => ({ useAuth: () => ({ user: null, loading: false }) }));

const MIN = 60_000;
/** The wall clock, and the instant a link opens on: 2026-10-18T20:00:00Z, sixteen hours before. */
const WALL = Date.UTC(2026, 9, 19, 12, 0, 0);
const T = Date.UTC(2026, 9, 18, 20, 0, 0);
/** The live edge at WALL: the latest instant the archive holds in full, and the latest the clock goes to. */
const EDGE = WALL - ARCHIVE_LAG_MS;
/** The window a 15-minute span loads around T: the fixtures' own [19:45Z, 20:15Z). */
const W = { from: T - 15 * MIN, to: T + 15 * MIN };
/** What window `w` serves, and windowRange spans: from the end of its lead-in to `end` (its own end, unless the lag or a cut comes first). */
const servable = (w: { from: number; to: number }, end = w.to) => ({ from: w.from + WINDOW_LEAD_IN_MS, to: end });
/** windowRange for W, whole and long past. */
const R = servable(W);

interface Call {
  params: { from: number; to: number; fleet?: string };
  signal: AbortSignal;
  resolve(resp: TracksResponse): void;
  reject(err: unknown): void;
}
let calls: Call[] = [];

const archiveTracks = vi.mocked(api.archiveTracks);
const hashWrites = vi.mocked(writeArchiveHash);

interface Props {
  active: boolean;
  t: number | null;
  spanMin: number;
  rev: number;
}

function setup(over: Partial<Props> = {}) {
  const initialProps: Props = { active: true, t: T, spanMin: 15, rev: 0, ...over };
  const hook = renderHook((p: Props) => useArchiveReplay(p), { initialProps });
  let props = initialProps;
  return {
    result: hook.result,
    rerender(next: Partial<Props>) {
      props = { ...props, ...next };
      hook.rerender(props);
    },
  };
}

/** Answers a request with `resp` and lets the hook take it in. */
async function respond(call: Call, resp: TracksResponse = KEYED.known) {
  await act(async () => {
    call.resolve(resp);
  });
}

async function fail(call: Call, err: unknown) {
  await act(async () => {
    call.reject(err);
  });
}

/** Subscribes to the hook's source, letting the subscription's own frame through. */
async function subscribe(result: { current: ArchiveReplay }) {
  const frames: InjectedFrame[] = [];
  let unsubscribe: () => void = () => {};
  await act(async () => {
    unsubscribe = result.current.source!.subscribe((frame) => frames.push(frame));
  });
  return { frames, unsubscribe: () => unsubscribe() };
}

const advance = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

/** Opens on T with the known fixture loaded and a subscriber that has had its first frame. */
async function opened(over: Partial<Props> = {}) {
  const h = setup(over);
  await respond(calls[0]);
  const sub = await subscribe(h.result);
  return { ...h, ...sub };
}

const lastOf = <T,>(xs: T[]): T | undefined => xs[xs.length - 1];
const nowsOf = (frames: InjectedFrame[]) => frames.map((f) => f.now * 1000);
const resetsOf = (frames: InjectedFrame[]) => frames.map((f) => f.reset);

/** The frame that clears the map at `ms`, an instant nothing loaded shows yet. */
const emptyAt = (ms: number): InjectedFrame => ({
  now: ms / 1000,
  aircraft: [],
  detection_arcs: [],
  detecting_nodes: {},
  anomaly_hexes: [],
  ground_truth: {},
  ground_truth_meta: {},
  reset: true,
});

/** `resp` moved `ms` later: the same tracks, solved that much later. */
function shifted(resp: TracksResponse, ms: number): TracksResponse {
  return {
    ...resp,
    from: resp.from + ms,
    to: resp.to + ms,
    rows: resp.rows.map((r) => ({ ...r, frame_ts_ms: r.frame_ts_ms + ms, solve_ts_ms: r.solve_ts_ms + ms })),
  };
}

/** KEYED.known moved so its last row has frame time `cut`, and the response cut there by the server. */
function cutAt(cut: number): TracksResponse {
  const last = Math.max(...KEYED.known.rows.map((r) => r.frame_ts_ms));
  return { ...shifted(KEYED.known, cut - last), truncated: true, count: 50_000 };
}

const TOO_DENSE = { kind: "other", message: "Too many solves around this instant to show." };

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(WALL);
  calls = [];
  archiveTracks.mockReset();
  archiveTracks.mockImplementation(
    (params, opts) =>
      new Promise<TracksResponse>((resolve, reject) => {
        calls.push({ params, signal: opts!.signal!, resolve, reject });
      }),
  );
  hashWrites.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("opening a link (R2, R3)", () => {
  it("requests [t - 15 min, t + 15 min) of the real fleet and opens paused at t", () => {
    const { result } = setup();
    expect(archiveTracks).toHaveBeenCalledTimes(1);
    expect(calls[0].params).toEqual({ from: W.from, to: W.to, fleet: "real" });
    expect(calls[0].signal.aborted).toBe(false);
    expect(result.current).toMatchObject({
      clockMs: T,
      playing: false,
      rate: 1,
      status: "loading",
      windowRange: R,
      error: null,
      frameNowMs: null,
    });
    expect(result.current.source).not.toBeNull();
  });

  it("centres the window on the clock floored to the second", () => {
    setup({ t: T + 1_750 });
    expect(calls[0].params).toEqual({ from: W.from + 1_000, to: W.to + 1_000, fleet: "real" });
  });

  it("clamps t to [2020-01-01, the live edge]", () => {
    const late = setup({ t: WALL + 3_600_000 });
    expect(late.result.current.clockMs).toBe(EDGE);
    cleanup();
    const early = setup({ t: Date.UTC(2015, 0, 1) });
    expect(early.result.current.clockMs).toBe(ARCHIVE_EARLIEST_MS);
  });

  it("holds the loaded window: ready, with its count and track count", async () => {
    const { result } = setup();
    await respond(calls[0]);
    expect(result.current).toMatchObject({
      status: "ready",
      windowRange: R,
      count: KEYED.known.count,
      truncated: false,
      trackCount: 2,
      error: null,
    });
  });

  it("propagates truncated", async () => {
    // A minute before T, which the fixture's rows reach past: the cut (its last
    // frame, 19:59:57.353Z) still serves the clock.
    const { result } = setup({ t: T - MIN });
    await respond(calls[0], { ...KEYED.known, truncated: true, count: 50_000 });
    expect(result.current).toMatchObject({ status: "ready", truncated: true, count: 50_000 });
  });

  it("reloads around the clock when the span changes", async () => {
    const { result, rerender } = setup();
    await respond(calls[0]);
    rerender({ spanMin: 5 });
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: T - 5 * MIN, to: T + 5 * MIN, fleet: "real" });
    expect(result.current).toMatchObject({ status: "loading", windowRange: servable({ from: T - 5 * MIN, to: T + 5 * MIN }) });
  });

  it("asks once for a link that changes both t and span", async () => {
    const { rerender } = setup();
    await respond(calls[0]);
    const t2 = T - 6 * 3_600_000;
    rerender({ rev: 1, t: t2, spanMin: 10 });
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: t2 - 10 * MIN, to: t2 + 10 * MIN, fleet: "real" });
  });

  it("aborts the request out when another window is asked for, and ignores its answer", async () => {
    const { result, rerender } = setup();
    const first = calls[0];
    const t2 = T - 6 * 3_600_000;
    const w2 = { from: t2 - 15 * MIN, to: t2 + 15 * MIN };
    rerender({ rev: 1, t: t2 });
    expect(first.signal.aborted).toBe(true);
    expect(calls).toHaveLength(2);
    await respond(first);
    expect(result.current).toMatchObject({ status: "loading", windowRange: servable(w2), clockMs: t2, count: 0 });
    await respond(calls[1]);
    expect(result.current).toMatchObject({ status: "ready", windowRange: servable(w2), count: KEYED.known.count });
  });

  it("seeks to the new t on a rev bump, paused, from the loaded window when it serves t", async () => {
    const { result, rerender, frames } = await opened();
    act(() => result.current.play());
    await advance(1_000);
    expect(result.current.playing).toBe(true);
    rerender({ rev: 1, t: T + 2 * MIN });
    expect(result.current).toMatchObject({ clockMs: T + 2 * MIN, playing: false, status: "ready" });
    expect(calls).toHaveLength(1);
    expect(lastOf(frames)).toMatchObject({ now: (T + 2 * MIN) / 1000, reset: true });
    await advance(1_000);
    expect(result.current.clockMs).toBe(T + 2 * MIN);
  });

  it("loads the window around a far t on a rev bump", async () => {
    const { result, rerender, frames } = await opened();
    const t2 = T - 6 * 3_600_000;
    const before = frames.length;
    rerender({ rev: 1, t: t2 });
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: t2 - 15 * MIN, to: t2 + 15 * MIN, fleet: "real" });
    expect(result.current).toMatchObject({ clockMs: t2, playing: false, status: "loading" });
    // The map is cleared at t2 at once (G2), so the window's frame there is no reset.
    expect(frames.slice(before)).toEqual([emptyAt(t2)]);
    await respond(calls[1]);
    expect(lastOf(frames)).toMatchObject({ now: t2 / 1000, reset: false });
  });

  it("does nothing for a null t", () => {
    const { result } = setup({ t: null });
    expect(archiveTracks).not.toHaveBeenCalled();
    expect(result.current.status).toBe("idle");
  });
});

describe("frames (R6, R8)", () => {
  it("hands a subscriber a reset frame at t on a microtask, with the fixture's aircraft", async () => {
    const { result } = setup();
    await respond(calls[0]);
    const frames: InjectedFrame[] = [];
    act(() => {
      result.current.source!.subscribe((frame) => frames.push(frame));
    });
    expect(frames).toHaveLength(0);
    await act(async () => {});
    expect(frames).toHaveLength(1);
    const expected = synthesizeFrame(buildReplayIndex(KEYED.known), T);
    expect(frames[0]).toEqual({ ...expected, reset: true });
    expect(frames[0].now).toBe(T / 1000);
    const known = KEYED.known.rows.find((r) => r.lane === "known").track_key;
    expect(frames[0].aircraft.map((a) => a.hex)).toContain(multinodeHexFromKey(known));
    expect(frames[0].aircraft).toHaveLength(2);
    expect(result.current.frameNowMs).toBe(T);
  });

  it("gives a subscriber that came before the window its reset frame when the window lands", async () => {
    const { result } = setup();
    const { frames } = await subscribe(result);
    expect(frames).toHaveLength(0);
    await respond(calls[0]);
    expect(resetsOf(frames)).toEqual([true]);
    expect(nowsOf(frames)).toEqual([T]);
  });

  it("keeps one subscriber: a second replaces the first, and an unsubscribed one hears nothing", async () => {
    const { result, frames, unsubscribe } = await opened();
    const second = await subscribe(result);
    expect(second.frames).toHaveLength(1);
    expect(second.frames[0].reset).toBe(true);
    const firstCount = frames.length;
    act(() => result.current.seek(T + 1_000));
    expect(frames).toHaveLength(firstCount);
    expect(second.frames).toHaveLength(2);
    // The first's unsubscribe no longer owns the source.
    unsubscribe();
    await advance(200);
    act(() => result.current.seek(T + 2_000));
    expect(second.frames).toHaveLength(3);
    second.unsubscribe();
    await advance(200);
    act(() => result.current.seek(T + 3_000));
    await act(async () => {});
    expect(second.frames).toHaveLength(3);
    expect(result.current.frameNowMs).toBe(T + 2_000);
  });

  it("resets on a jump back or past 10 s, and not on a step forward within it", async () => {
    const { result, frames } = await opened();
    frames.length = 0;
    const seekTo = async (ms: number) => {
      act(() => result.current.seek(ms));
      await advance(200);
    };
    await seekTo(T - 1_000); // back 1 s
    await seekTo(T + 2_000); // +3 s
    await seekTo(T + 12_000); // +10 s exactly
    await seekTo(T + 22_001); // +10.001 s
    await seekTo(T + 82_001); // +60 s
    await seekTo(T + 82_001); // the same instant again
    expect(nowsOf(frames)).toEqual([T - 1_000, T + 2_000, T + 12_000, T + 22_001, T + 82_001, T + 82_001]);
    expect(resetsOf(frames)).toEqual([true, false, false, true, true, false]);
  });

  it("shows nothing from the loaded window at an instant it cannot fully show: clears the map there and loads the window around it", async () => {
    const { result, frames } = await opened();
    const early = W.from + WINDOW_LEAD_IN_MS - 1_000;
    const before = frames.length;
    act(() => result.current.seek(early));
    expect(frames.slice(before)).toEqual([emptyAt(early)]);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: early - 15 * MIN, to: early + 15 * MIN, fleet: "real" });
    await respond(calls[1]);
    expect(lastOf(nowsOf(frames))).toBe(early);
  });

  it("gives a subscriber nothing while the clock waits for its window, then a reset frame when it lands", async () => {
    const { result } = await opened();
    const far = T + 3 * 3_600_000;
    act(() => result.current.seek(far));
    const late = await subscribe(result);
    expect(late.frames).toHaveLength(0);
    // The first subscriber's map was cleared at `far` (G2).
    expect(result.current.frameNowMs).toBe(far);
    await respond(calls[1]);
    expect(late.frames).toHaveLength(1);
    expect(late.frames[0]).toMatchObject({ now: far / 1000, reset: true });
  });

  it("serves an instant at the end of the lead-in from the loaded window", async () => {
    const { result, frames } = await opened();
    act(() => result.current.seek(W.from + WINDOW_LEAD_IN_MS));
    expect(calls).toHaveLength(1);
    expect(lastOf(nowsOf(frames))).toBe(W.from + WINDOW_LEAD_IN_MS);
  });
});

describe("playback (R5)", () => {
  it("plays at 5x: a wall second moves the clock 5 s, with at most 5 frames and no reset", async () => {
    const { result, frames } = await opened();
    frames.length = 0;
    act(() => {
      result.current.setRate(5);
      result.current.play();
    });
    expect(result.current).toMatchObject({ playing: true, rate: 5 });
    await advance(1_000);
    expect(result.current.clockMs).toBe(T + 5_000);
    expect(frames.length).toBeGreaterThan(0);
    expect(frames.length).toBeLessThanOrEqual(5);
    expect(nowsOf(frames)).toEqual([T + 1_250, T + 2_500, T + 3_750, T + 5_000]);
    expect(resetsOf(frames).every((r) => r === false)).toBe(true);
    expect(result.current.frameNowMs).toBe(T + 5_000);
  });

  it("emits at most 4 frames per wall second at 20x", async () => {
    const { result, frames } = await opened();
    frames.length = 0;
    act(() => {
      result.current.setRate(20);
      result.current.play();
    });
    await advance(1_000);
    expect(frames).toHaveLength(4);
    expect(result.current.clockMs).toBe(T + 20_000);
  });

  it("at the end of the window asks once for the window around the clock, holds the clock while it loads, then swaps without a reset", async () => {
    const { result, frames } = await opened();
    act(() => result.current.seek(W.to - 2_000));
    frames.length = 0;
    act(() => result.current.play());
    await advance(250);
    // The first tick is inside the last minute, so the next window is asked for.
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: W.to - 2_000 - 15 * MIN, to: W.to - 2_000 + 15 * MIN, fleet: "real" });
    await advance(5_000);
    // Seven ticks fit before W.to; the eighth would leave the window, so the clock holds.
    expect(result.current).toMatchObject({ clockMs: W.to - 250, playing: true, status: "loading" });
    expect(lastOf(nowsOf(frames))).toBe(W.to - 250);
    const held = frames.length;
    await advance(2_000);
    expect(result.current.clockMs).toBe(W.to - 250);
    expect(frames).toHaveLength(held);
    expect(calls).toHaveLength(2);

    await respond(calls[1]);
    expect(result.current).toMatchObject({
      status: "ready",
      windowRange: servable(calls[1].params),
    });
    await advance(500);
    expect(result.current.clockMs).toBe(W.to + 250);
    expect(nowsOf(frames).slice(held)).toEqual([W.to - 250, W.to, W.to + 250]);
    expect(resetsOf(frames).every((r) => r === false)).toBe(true);
  });

  it("prefetches at 20x five minutes before the end, plays on from the old window, and swaps without a reset", async () => {
    const { result, frames } = await opened();
    act(() => result.current.seek(T + 590_000));
    frames.length = 0;
    act(() => {
      result.current.setRate(20);
      result.current.play();
    });
    await advance(250);
    expect(calls).toHaveLength(1);
    await advance(250);
    // At T + 600 s the clock is 300 s (15 s x 20) from the end.
    expect(result.current.clockMs).toBe(T + 600_000);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: T + 600_000 - 15 * MIN, to: T + 600_000 + 15 * MIN, fleet: "real" });
    expect(result.current.windowRange).toEqual(servable(calls[1].params));

    await advance(1_000);
    expect(result.current.clockMs).toBe(T + 620_000);
    expect(lastOf(nowsOf(frames))).toBe(T + 620_000);
    expect(calls).toHaveLength(2);

    await respond(calls[1]);
    // Sixteen more seconds at 20x carry the clock past the old window's end.
    await advance(16_000);
    expect(result.current.clockMs).toBe(T + 940_000);
    expect(result.current.clockMs).toBeGreaterThan(W.to);
    expect(calls).toHaveLength(2);
    expect(resetsOf(frames).every((r) => r === false)).toBe(true);
  });

  it("asks for the window around the clock when play starts where nothing is loaded or out, and holds until it lands", async () => {
    const { result, frames } = await opened();
    const far = T - 2 * 3_600_000;
    act(() => result.current.seek(far));
    await fail(calls[1], new HttpError(503, "Service Unavailable", null));
    expect(result.current.status).toBe("error");
    act(() => result.current.play());
    await advance(250);
    expect(calls).toHaveLength(3);
    expect(calls[2].params).toEqual({ from: far - 15 * MIN, to: far + 15 * MIN, fleet: "real" });
    await advance(1_000);
    expect(result.current).toMatchObject({ clockMs: far, playing: true, status: "loading" });
    expect(calls).toHaveLength(3);
    await respond(calls[2]);
    expect(lastOf(nowsOf(frames))).toBe(far);
    await advance(250);
    expect(result.current.clockMs).toBe(far + 250);
  });

  it("stops short of the live edge, on the last instant its window serves, and a play there asks once for the window around the clock", async () => {
    const { result } = setup({ t: EDGE - 3_000 });
    await respond(calls[0]);
    act(() => {
      result.current.setRate(5);
      result.current.play();
    });
    await advance(500);
    expect(result.current).toMatchObject({ clockMs: EDGE - 500, playing: true });
    await advance(250);
    // The next tick would pass EDGE, the last instant the window serves, with
    // the archive holding under a flush period past the clock (G1).
    expect(result.current).toMatchObject({ clockMs: EDGE - 500, playing: false });
    expect(calls).toHaveLength(1);
    act(() => result.current.play());
    expect(result.current.playing).toBe(true);
    await advance(250);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: EDGE - 1_000 - 15 * MIN, to: EDGE - 1_000 + 15 * MIN, fleet: "real" });
    expect(result.current).toMatchObject({ clockMs: EDGE - 500, playing: true, status: "loading" });
  });

  it("does not start within a second of the live edge", async () => {
    const { result } = setup({ t: EDGE - 1_000 });
    await respond(calls[0]);
    act(() => result.current.play());
    expect(result.current.playing).toBe(false);
    act(() => result.current.togglePlay());
    expect(result.current.playing).toBe(false);
  });

  it("toggles play and pause; a pause emits nothing", async () => {
    const { result, frames } = await opened();
    act(() => result.current.togglePlay());
    expect(result.current.playing).toBe(true);
    await advance(500);
    const count = frames.length;
    act(() => result.current.togglePlay());
    expect(result.current.playing).toBe(false);
    expect(frames).toHaveLength(count);
    await advance(1_000);
    expect(frames).toHaveLength(count);
    expect(result.current.clockMs).toBe(T + 500);
  });
});

describe("seeks and steps (R7)", () => {
  it("lands rapid seeks at most once per 200 ms, the last one always", async () => {
    const { result, frames } = await opened();
    frames.length = 0;
    act(() => result.current.seek(T + 1_000));
    expect(nowsOf(frames)).toEqual([T + 1_000]);
    for (const ms of [T + 2_000, T + 3_000, T + 4_000]) {
      act(() => result.current.seek(ms));
      expect(result.current.clockMs).toBe(ms);
    }
    expect(frames).toHaveLength(1);
    await advance(199);
    expect(frames).toHaveLength(1);
    await advance(1);
    expect(nowsOf(frames)).toEqual([T + 1_000, T + 4_000]);
    await advance(50);
    act(() => result.current.seek(T + 5_000));
    await advance(149);
    expect(frames).toHaveLength(2);
    await advance(1);
    expect(nowsOf(frames)).toEqual([T + 1_000, T + 4_000, T + 5_000]);
    expect(hashWrites.mock.calls).toEqual([[T + 1_000], [T + 4_000], [T + 5_000]]);
  });

  it("clamps a seek to [2020-01-01, the live edge]", async () => {
    const { result } = await opened();
    act(() => result.current.seek(WALL + 3_600_000));
    expect(result.current.clockMs).toBe(EDGE);
    await advance(200);
    act(() => result.current.seek(Date.UTC(2012, 0, 1)));
    expect(result.current.clockMs).toBe(ARCHIVE_EARLIEST_MS);
  });

  it("keeps playing through a seek", async () => {
    const { result } = await opened();
    act(() => result.current.play());
    await advance(500);
    act(() => result.current.seek(T + 60_000));
    expect(result.current.playing).toBe(true);
    await advance(500);
    expect(result.current.clockMs).toBe(T + 60_500);
  });

  it("steps by pausing and moving the clock by whole seconds", async () => {
    const { result, frames } = await opened();
    act(() => result.current.play());
    await advance(500);
    act(() => result.current.step(1));
    expect(result.current).toMatchObject({ playing: false, clockMs: T + 1_500 });
    expect(lastOf(nowsOf(frames))).toBe(T + 1_500);
    act(() => result.current.step(-1));
    expect(result.current).toMatchObject({ playing: false, clockMs: T + 500 });
    await advance(200);
    expect(lastOf(nowsOf(frames))).toBe(T + 500);
    expect(lastOf(frames)?.reset).toBe(true);
    await advance(1_000);
    expect(result.current.clockMs).toBe(T + 500);
  });

  it("loads the window around a seek outside the loaded one", async () => {
    const { result, frames } = await opened();
    const far = T + 3 * 3_600_000;
    const before = frames.length;
    act(() => result.current.seek(far));
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: far - 15 * MIN, to: far + 15 * MIN, fleet: "real" });
    expect(result.current).toMatchObject({ clockMs: far, status: "loading", windowRange: servable({ from: far - 15 * MIN, to: far + 15 * MIN }) });
    expect(frames.slice(before)).toEqual([emptyAt(far)]);
  });

  it("drops a request out for elsewhere when a seek comes back into the loaded window", async () => {
    const { result, frames } = await opened();
    act(() => result.current.seek(T + 3 * 3_600_000));
    await advance(200);
    act(() => result.current.seek(T + 5_000));
    expect(calls[1].signal.aborted).toBe(true);
    expect(result.current).toMatchObject({ status: "ready", windowRange: R });
    expect(lastOf(nowsOf(frames))).toBe(T + 5_000);
    await respond(calls[1]);
    expect(result.current).toMatchObject({ status: "ready", windowRange: R, clockMs: T + 5_000 });
  });

  it("waits for a request out that serves the seek instead of asking again", async () => {
    const { result } = await opened();
    const far = T + 3 * 3_600_000;
    act(() => result.current.seek(far));
    await advance(200);
    act(() => result.current.seek(far + 60_000));
    expect(calls).toHaveLength(2);
    expect(calls[1].signal.aborted).toBe(false);
  });
});

/** Answers every request as soon as it is asked; the requests are read off the mock. */
function answerAtOnce() {
  archiveTracks.mockImplementation(() => Promise.resolve(KEYED.known));
}
const centresAsked = () => archiveTracks.mock.calls.map(([p]) => (p.from + p.to) / 2);

describe("the prefetch margin (F1)", () => {
  it("asks once per window change at 20x on a one-minute radius: every 30 s of replay, each centre once, never holding", async () => {
    answerAtOnce();
    const { result } = setup({ spanMin: 1 });
    await act(async () => {});
    expect(result.current.status).toBe("ready");
    act(() => {
      result.current.setRate(20);
      result.current.play();
    });
    await advance(60_000);
    // The margin is min(max(60 s, 15 s x 20), 60 s / 2) = 30 s: a window around c
    // serves from c (its lead-in is the whole first half) and is due at c + 30 s.
    // 240 ticks of 5 s are 1,200 s of replay, so 40 windows follow the first.
    expect(centresAsked()).toEqual(Array.from({ length: 41 }, (_, i) => T + i * 30_000));
    expect(result.current).toMatchObject({ clockMs: T + 1_200_000, playing: true, status: "ready" });
  });
});

describe("the live edge (F2)", () => {
  it("opens a link into the last two minutes at the live edge", () => {
    const { result } = setup({ t: WALL - 30_000 });
    expect(result.current.clockMs).toBe(EDGE);
    expect(calls[0].params).toEqual({ from: EDGE - 15 * MIN, to: EDGE + 15 * MIN, fleet: "real" });
  });

  it("clamps a new link, a seek and a step to the live edge", async () => {
    const edgeNow = () => Date.now() - ARCHIVE_LAG_MS;
    const { result, rerender } = await opened();
    rerender({ rev: 1, t: WALL - 1_000 });
    expect(result.current.clockMs).toBe(edgeNow());
    await advance(200);
    act(() => result.current.seek(WALL));
    expect(result.current.clockMs).toBe(edgeNow());
    await advance(200);
    act(() => result.current.seek(edgeNow() - 10_000));
    expect(result.current.clockMs).toBe(edgeNow() - 10_000);
    act(() => result.current.step(60));
    expect(result.current).toMatchObject({ clockMs: edgeNow(), playing: false });
  });

  it("plays from 5 s before the edge at 5x and stops as a pause does on the last instant its window serves, asking for nothing", async () => {
    const { result, frames } = await opened({ t: EDGE - 5_000 });
    frames.length = 0;
    act(() => {
      result.current.setRate(5);
      result.current.play();
    });
    await advance(1_000);
    // Four ticks reach the edge as it stood when the window was fetched: the last instant it serves.
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: true });
    expect(nowsOf(frames)).toEqual([EDGE - 3_750, EDGE - 2_500, EDGE - 1_250, EDGE]);
    expect(calls).toHaveLength(1);

    await advance(250);
    // The fifth would pass it, with the archive holding only 1.25 s more (G1):
    // play stops on it, and the URL follows as for a pause.
    expect(result.current).toMatchObject({ clockMs: EDGE, frameNowMs: EDGE, playing: false, status: "ready" });
    expect(hashWrites.mock.calls).toEqual([[EDGE]]);
    expect(calls).toHaveLength(1);
    expect(frames).toHaveLength(4);

    await advance(60_000);
    expect(calls).toHaveLength(1);
    expect(frames).toHaveLength(4);
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: false, status: "ready" });
  });

  it("plays at 1x from 5 s before the edge to the last instant its window serves and stops there: no request besides the first", async () => {
    answerAtOnce();
    const { result } = setup({ t: EDGE - 5_000 });
    await act(async () => {});
    act(() => result.current.play());
    await advance(60_000);
    // The window serves up to its fetch less the lag, EDGE, which tick 20
    // reaches. Tick 21 would pass it with the archive holding only 5.25 s more,
    // so play stops there (G1) instead of asking again every few seconds.
    expect(centresAsked()).toEqual([EDGE - 5_000]);
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: false, status: "ready" });
  });
});

/** A link two hours ahead: the reviewer's repro. */
const AHEAD = WALL + 2 * 3_600_000;
const BEFORE_ARCHIVE = Date.UTC(2015, 0, 1);

describe("play at the live edge (L1)", () => {
  it("is at the edge on a link past it, where play and its toggle do nothing", async () => {
    const { result, frames } = await opened({ t: AHEAD });
    expect(result.current).toMatchObject({ clockMs: EDGE, atLiveEdge: true, playing: false, status: "ready" });
    const before = frames.length;
    act(() => result.current.play());
    act(() => result.current.togglePlay());
    expect(result.current).toMatchObject({ clockMs: EDGE, atLiveEdge: true, playing: false });
    await advance(LIVE_EDGE_MS);
    expect(result.current).toMatchObject({ clockMs: EDGE, atLiveEdge: true, playing: false });
    expect(frames).toHaveLength(before);
    expect(calls).toHaveLength(1);
  });

  it("is not at the edge off it, nor while inactive", () => {
    const { result, rerender } = setup();
    expect(result.current.atLiveEdge).toBe(false);
    rerender({ active: false });
    expect(result.current.atLiveEdge).toBe(false);
  });

  it("leaves the edge on its own once the edge has moved a second past the clock, and play then starts", async () => {
    const { result } = await opened({ t: AHEAD });
    await advance(LIVE_EDGE_MS);
    expect(result.current.atLiveEdge).toBe(true);
    await advance(1);
    expect(result.current).toMatchObject({ clockMs: EDGE, atLiveEdge: false, playing: false });
    act(() => result.current.play());
    expect(result.current.playing).toBe(true);
  });

  it("stops when playback reaches the edge, and is at it", async () => {
    const { result } = await opened({ t: EDGE - 5_000 });
    act(() => {
      result.current.setRate(20);
      result.current.play();
    });
    await advance(250);
    // On the last instant the window serves, a quarter second short of where the edge now is.
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: true, atLiveEdge: true });
    await advance(250);
    // The next tick would pass it: play stops there, still within a second of the edge.
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: false, atLiveEdge: true });
    act(() => result.current.togglePlay());
    expect(result.current.playing).toBe(false);
  });

  it("stops on the live edge itself when a seek puts playback there, and is at it", async () => {
    const { result } = await opened();
    act(() => result.current.play());
    act(() => result.current.seek(WALL));
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: true });
    await advance(250);
    expect(result.current).toMatchObject({ clockMs: EDGE + 250, playing: false, atLiveEdge: true });
    act(() => result.current.play());
    expect(result.current.playing).toBe(false);
  });

  it("leaves the edge on a step back", async () => {
    const { result } = await opened({ t: AHEAD });
    await advance(100);
    act(() => result.current.step(-1));
    expect(result.current).toMatchObject({ clockMs: EDGE - 1_000, atLiveEdge: false, playing: false });
    act(() => result.current.play());
    expect(result.current.playing).toBe(true);
  });

  it("leaves the edge on a seek back, and is back at it on a seek past it", async () => {
    const { result } = await opened({ t: AHEAD });
    act(() => result.current.seek(EDGE - 10_000));
    expect(result.current).toMatchObject({ clockMs: EDGE - 10_000, atLiveEdge: false });
    await advance(200);
    act(() => result.current.seek(AHEAD));
    expect(result.current).toMatchObject({ clockMs: EDGE + 200, atLiveEdge: true });
  });

  it("leaves no timer behind when the replay ends at the edge", async () => {
    const { result, rerender } = await opened({ t: AHEAD });
    expect(result.current.atLiveEdge).toBe(true);
    rerender({ active: false });
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("a link or seek the archive cannot show (L2)", () => {
  it("opens a link past the live edge on the edge, names it in the URL at once, and says so", () => {
    const { result } = setup({ t: AHEAD });
    expect(result.current).toMatchObject({ clockMs: EDGE, clamp: { by: "link", askedMs: AHEAD } });
    expect(hashWrites.mock.calls).toEqual([[EDGE]]);
  });

  it("opens a link before the archive on its first instant, names it in the URL at once, and says so", () => {
    const { result } = setup({ t: BEFORE_ARCHIVE });
    expect(result.current).toMatchObject({ clockMs: ARCHIVE_EARLIEST_MS, clamp: { by: "link", askedMs: BEFORE_ARCHIVE } });
    expect(hashWrites.mock.calls).toEqual([[ARCHIVE_EARLIEST_MS]]);
  });

  it("does the same for a hashchange to such an instant, and a link it can show clears it", async () => {
    const { result, rerender } = await opened();
    expect(result.current.clamp).toBeNull();
    rerender({ rev: 1, t: AHEAD });
    expect(result.current).toMatchObject({ clockMs: EDGE, clamp: { by: "link", askedMs: AHEAD } });
    expect(hashWrites.mock.calls).toEqual([[EDGE]]);
    rerender({ rev: 2, t: T });
    expect(result.current).toMatchObject({ clockMs: T, clamp: null });
    expect(hashWrites).toHaveBeenCalledTimes(1);
  });

  it("says so for a seek or a step past either end, which lands on the instant shown", async () => {
    const { result } = await opened();
    act(() => result.current.seek(AHEAD));
    expect(result.current).toMatchObject({ clockMs: EDGE, clamp: { by: "seek", askedMs: AHEAD } });
    expect(lastOf(hashWrites.mock.calls)).toEqual([EDGE]);
    await advance(200);
    act(() => result.current.seek(BEFORE_ARCHIVE));
    expect(result.current).toMatchObject({ clockMs: ARCHIVE_EARLIEST_MS, clamp: { by: "seek", askedMs: BEFORE_ARCHIVE } });
    expect(lastOf(hashWrites.mock.calls)).toEqual([ARCHIVE_EARLIEST_MS]);
    await advance(200);
    act(() => result.current.step(-1));
    expect(result.current).toMatchObject({
      clockMs: ARCHIVE_EARLIEST_MS,
      clamp: { by: "seek", askedMs: ARCHIVE_EARLIEST_MS - 1_000 },
    });
  });

  it("clears on the next seek", async () => {
    const { result } = await opened({ t: AHEAD });
    act(() => result.current.seek(T));
    expect(result.current).toMatchObject({ clockMs: T, clamp: null });
  });

  it("clears on the next step", async () => {
    const { result } = await opened({ t: AHEAD });
    await advance(100);
    act(() => result.current.step(-1));
    expect(result.current).toMatchObject({ clockMs: EDGE - 1_000, clamp: null });
  });

  it("clears on the next play, and stays through what does not move the clock", async () => {
    const { result, rerender } = await opened({ t: BEFORE_ARCHIVE });
    const clamp = { by: "link", askedMs: BEFORE_ARCHIVE };
    act(() => result.current.setRate(5));
    rerender({ spanMin: 5 });
    await respond(calls[1]);
    act(() => result.current.pause());
    expect(result.current).toMatchObject({ clockMs: ARCHIVE_EARLIEST_MS, clamp });
    act(() => result.current.play());
    expect(result.current).toMatchObject({ playing: true, clamp: null });
  });

  it("clears once playback moves the clock on from where a seek put it", async () => {
    const { result } = await opened();
    act(() => result.current.play());
    act(() => result.current.seek(BEFORE_ARCHIVE));
    expect(result.current).toMatchObject({ playing: true, clamp: { by: "seek", askedMs: BEFORE_ARCHIVE } });
    await respond(calls[1]);
    expect(result.current.clamp).not.toBeNull();
    await advance(250);
    expect(result.current).toMatchObject({ playing: true, clockMs: ARCHIVE_EARLIEST_MS + 250, clamp: null });
  });

  it("is never set by a link or seek the archive can show", async () => {
    const { result } = await opened({ t: EDGE });
    expect(result.current.clamp).toBeNull();
    act(() => result.current.seek(T));
    expect(result.current.clamp).toBeNull();
    expect(hashWrites.mock.calls).toEqual([[T]]);
  });
});

describe("the archive's lag (F3)", () => {
  it("plays a window fetched while it reached past the archive to its fetch less 120 s, then holds, asks once for the window around the clock, and plays on without a reset", async () => {
    const { result, frames } = await opened({ t: EDGE - 60_000 });
    // Fetched at WALL, it serves up to EDGE, though it runs on to EDGE + 14 min:
    // the scrubber spans [EDGE - 15 min, EDGE], past the lead-in.
    expect(result.current.windowRange).toEqual({ from: EDGE - 15 * MIN, to: EDGE });
    // Played 50 s later, the clock reaches EDGE with the archive 62 s past it:
    // too little for a prefetch at 5x (75 s), but a flush period (G1).
    await advance(50_000);
    frames.length = 0;
    act(() => {
      result.current.setRate(5);
      result.current.play();
    });
    await advance(12_000);
    // Forty-eight ticks of 1.25 s reach EDGE, the last instant it serves, with nothing asked for on the way.
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: true, status: "ready" });
    expect(lastOf(nowsOf(frames))).toBe(EDGE);
    expect(calls).toHaveLength(1);

    await advance(250);
    // The next would pass it: the clock holds, and the window around it is asked for.
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: EDGE - 15 * MIN, to: EDGE + 15 * MIN, fleet: "real" });
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: true, status: "loading" });
    const held = frames.length;
    await advance(2_000);
    expect(result.current.clockMs).toBe(EDGE);
    expect(frames).toHaveLength(held);
    expect(calls).toHaveLength(2);

    await respond(calls[1]);
    await advance(1_000);
    expect(result.current).toMatchObject({ clockMs: EDGE + 5_000, playing: true, status: "ready" });
    expect(nowsOf(frames).slice(held)).toEqual([EDGE, EDGE + 1_250, EDGE + 2_500, EDGE + 3_750, EDGE + 5_000]);
    expect(resetsOf(frames).every((r) => r === false)).toBe(true);
    expect(calls).toHaveLength(2);
  });

  it("loads the window around a seek past where the loaded one stops serving, though inside its range", async () => {
    const { result, frames } = await opened({ t: EDGE - 60_000 });
    await advance(30_000);
    act(() => result.current.seek(EDGE - 1_000));
    expect(calls).toHaveLength(1);
    expect(lastOf(nowsOf(frames))).toBe(EDGE - 1_000);
    await advance(200);
    const before = frames.length;
    act(() => result.current.seek(EDGE + 20_000));
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: EDGE + 20_000 - 15 * MIN, to: EDGE + 20_000 + 15 * MIN, fleet: "real" });
    expect(frames.slice(before)).toEqual([emptyAt(EDGE + 20_000)]);
    await respond(calls[1]);
    expect(lastOf(frames)).toMatchObject({ now: (EDGE + 20_000) / 1000 });
  });

  it("serves a window that ends before the archive's edge right up to its end, and prefetches a minute before it as before", async () => {
    // It ends 10 minutes before the edge, so the lag cuts nothing off it.
    const t = EDGE - 25 * MIN;
    const end = t + 15 * MIN;
    const { result, frames } = await opened({ t });
    act(() => result.current.seek(end - 61_000));
    act(() => result.current.play());
    await advance(750);
    expect(calls).toHaveLength(1);
    await advance(250);
    expect(result.current.clockMs).toBe(end - 60_000);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: end - 60_000 - 15 * MIN, to: end - 60_000 + 15 * MIN, fleet: "real" });
    // Unanswered, the clock plays on from the loaded window to its last tick before the end, and holds there.
    await advance(60_000);
    expect(result.current).toMatchObject({ clockMs: end - 250, playing: true });
    expect(lastOf(nowsOf(frames))).toBe(end - 250);
    expect(calls).toHaveLength(2);
  });
});

describe("playing into the archive's edge (G1)", () => {
  it("plays at 1x from 30 s before the edge to the last instant its window serves and stops there, asking for nothing in a minute", async () => {
    const { result, frames } = await opened({ t: EDGE - 30_000 });
    act(() => result.current.play());
    await advance(60_000);
    // Fetched at WALL, the window serves up to EDGE, which tick 120 reaches.
    // Tick 121 would pass it with the archive holding 30.25 s more, under a
    // flush period: play stops on EDGE as a pause would.
    expect(calls).toHaveLength(1);
    expect(result.current).toMatchObject({ clockMs: EDGE, frameNowMs: EDGE, playing: false, status: "ready" });
    expect(lastOf(frames)).toEqual({ ...synthesizeFrame(buildReplayIndex(KEYED.known), EDGE), reset: false });
    // Ten writes every 3 s of play (ticks 12 to 120), then the stop's own.
    expect(hashWrites).toHaveBeenCalledTimes(11);
    expect(hashWrites.mock.calls.slice(-2)).toEqual([[EDGE], [EDGE]]);
  });

  it("after such a stop, a play asks once for the window around the clock, plays what it adds and stops again", async () => {
    const { result, frames } = await opened({ t: EDGE - 30_000 });
    act(() => result.current.play());
    await advance(31_000);
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: false });
    expect(calls).toHaveLength(1);

    // The archive now holds 31.25 s past the clock: under a flush period, but a play may ask once.
    act(() => result.current.play());
    await advance(250);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: EDGE - 15 * MIN, to: EDGE + 15 * MIN, fleet: "real" });
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: true, status: "loading" });

    await respond(calls[1]);
    await advance(60_000);
    // Fetched 31.25 s after WALL, it serves to EDGE + 31.25 s, which 125 more
    // ticks reach; the one after would pass it with the archive 31.5 s ahead.
    expect(calls).toHaveLength(2);
    expect(result.current).toMatchObject({ clockMs: EDGE + 31_250, frameNowMs: EDGE + 31_250, playing: false, status: "ready" });
    expect(lastOf(nowsOf(frames))).toBe(EDGE + 31_250);
    expect(resetsOf(frames).slice(1).every((r) => r === false)).toBe(true);
  });

  it("plays at 1x from 10 minutes before the edge, asking for the next window only when it adds a flush period or more: four requests in 30 minutes", async () => {
    const askedAt: number[] = [];
    archiveTracks.mockImplementation(() => {
      askedAt.push(Date.now());
      return Promise.resolve(KEYED.known);
    });
    const { result } = setup({ t: EDGE - 10 * MIN });
    await act(async () => {});
    act(() => result.current.play());
    await advance(30 * MIN);
    // Each window serves to its fetch less the lag. The clock trails the edge by
    // 10 min, so a minute (the margin at 1x) before one stops serving, the window
    // around the clock serves 9 min further: one at 0, 9, 18 and 27 min of play.
    expect(centresAsked()).toEqual([-10, -1, 8, 17].map((m) => EDGE + m * MIN));
    expect(askedAt.map((w) => w - WALL)).toEqual([0, 9, 18, 27].map((m) => m * MIN));
    // What each adds: its servable end less the one before it's.
    const gains = askedAt.slice(1).map((w, i) => w - askedAt[i]);
    expect(gains.every((g) => g >= FLUSH_PERIOD_MS)).toBe(true);
    expect(result.current).toMatchObject({ clockMs: EDGE + 20 * MIN, playing: true, status: "ready" });
  });

  it("plays at 20x into the edge and stops on the last instant its window serves, asking for nothing", async () => {
    const { result, frames } = await opened({ t: EDGE - 55_000 });
    act(() => {
      result.current.setRate(20);
      result.current.play();
    });
    await advance(2_750);
    // Eleven ticks of 5 s reach EDGE, the last instant the window serves.
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: true });
    await advance(250);
    // The twelfth would carry the clock to the live edge, 3 s past what the
    // window serves: play stops on EDGE instead.
    expect(result.current).toMatchObject({ clockMs: EDGE, frameNowMs: EDGE, playing: false, status: "ready" });
    expect(lastOf(nowsOf(frames))).toBe(EDGE);
    expect(hashWrites.mock.calls).toEqual([[EDGE]]);
    await advance(60_000);
    expect(calls).toHaveLength(1);
    expect(result.current).toMatchObject({ clockMs: EDGE, playing: false });
  });
});

describe("jumps to an instant nothing loaded serves (G2)", () => {
  const FAR = T + 3 * 3_600_000;
  /** The fixture's tracks around FAR, and the frame they show there. */
  const atFar = shifted(KEYED.known, FAR - T);
  const frameAtFar = () => ({ ...synthesizeFrame(buildReplayIndex(atFar), FAR), reset: false });

  it("a seek clears the map at the new clock before its window lands, then shows that window's frame there with no second reset", async () => {
    const { result, frames } = await opened();
    frames.length = 0;
    act(() => result.current.seek(FAR));
    expect(calls).toHaveLength(2);
    expect(frames).toEqual([emptyAt(FAR)]);
    expect(result.current).toMatchObject({ clockMs: FAR, frameNowMs: FAR, status: "loading" });
    await respond(calls[1], atFar);
    expect(frameAtFar().aircraft).toHaveLength(2);
    expect(frames).toEqual([emptyAt(FAR), frameAtFar()]);
    expect(result.current.frameNowMs).toBe(FAR);
  });

  it("a new link to such an instant does the same", async () => {
    const { result, rerender, frames } = await opened();
    frames.length = 0;
    rerender({ rev: 1, t: FAR });
    expect(calls).toHaveLength(2);
    expect(frames).toEqual([emptyAt(FAR)]);
    expect(result.current).toMatchObject({ clockMs: FAR, frameNowMs: FAR, playing: false, status: "loading" });
    await respond(calls[1], atFar);
    expect(frames).toEqual([emptyAt(FAR), frameAtFar()]);
  });

  it("a hold during playback delivers nothing", async () => {
    const { result, frames } = await opened();
    act(() => result.current.play());
    await advance(500);
    frames.length = 0;
    act(() => result.current.seek(FAR));
    expect(frames).toEqual([emptyAt(FAR)]);
    await advance(2_000);
    // Eight ticks held the clock for the window out, none of them with a frame.
    expect(frames).toEqual([emptyAt(FAR)]);
    expect(result.current).toMatchObject({ clockMs: FAR, frameNowMs: FAR, playing: true, status: "loading" });
    await respond(calls[1], atFar);
    await advance(250);
    expect(nowsOf(frames)).toEqual([FAR, FAR, FAR + 250]);
    expect(resetsOf(frames)).toEqual([true, false, false]);
  });

  it("rapid far seeks clear the map once per coalescing slot, at the seek that lands", async () => {
    const { result, frames } = await opened();
    frames.length = 0;
    const [a, b, c] = [FAR - 3_600_000, FAR + 3_600_000, FAR];
    act(() => result.current.seek(a));
    act(() => result.current.seek(b));
    act(() => result.current.seek(c));
    expect(frames).toEqual([emptyAt(a)]);
    await advance(199);
    expect(frames).toEqual([emptyAt(a)]);
    await advance(1);
    expect(frames).toEqual([emptyAt(a), emptyAt(c)]);
    expect(calls).toHaveLength(3);
    expect(calls[1].signal.aborted).toBe(true);
    await respond(calls[2], atFar);
    expect(frames).toEqual([emptyAt(a), emptyAt(c), frameAtFar()]);
    expect(result.current.frameNowMs).toBe(c);
  });

  it("a stop at the live edge, where nothing loaded serves, clears the map there and asks for nothing more", async () => {
    const { result, frames } = await opened();
    act(() => {
      result.current.setRate(5);
      result.current.play();
    });
    await advance(250);
    frames.length = 0;
    // A seek past the edge lands on it, where the window loaded around T has nothing.
    act(() => result.current.seek(WALL));
    expect(result.current.clockMs).toBe(EDGE + 250);
    expect(calls).toHaveLength(2);
    expect(frames).toEqual([emptyAt(EDGE + 250)]);
    await advance(250);
    // The next tick reaches the edge as it stands now: play stops there, on an
    // instant nothing loaded serves, which the window out does.
    expect(result.current).toMatchObject({ clockMs: EDGE + 500, frameNowMs: EDGE + 500, playing: false });
    expect(frames).toEqual([emptyAt(EDGE + 250), emptyAt(EDGE + 500)]);
    expect(calls).toHaveLength(2);
    await respond(calls[1]);
    expect(frames).toHaveLength(3);
    expect(lastOf(frames)).toMatchObject({ now: (EDGE + 500) / 1000, reset: false });
  });
});

describe("the scrubber's range (B3)", () => {
  it("spans what the loaded window serves, from the end of its lead-in, and a seek to either end of the scrubber is served from it with no request", async () => {
    const { result, frames } = await opened();
    expect(result.current.windowRange).toEqual({ from: W.from + WINDOW_LEAD_IN_MS, to: W.to });
    act(() => result.current.seek(result.current.windowRange!.from));
    expect(calls).toHaveLength(1);
    expect(lastOf(frames)).toMatchObject({ now: (W.from + WINDOW_LEAD_IN_MS) / 1000 });
    await advance(200);
    // The scrubber ends a second before the window's own end, which the API leaves out.
    act(() => result.current.seek(result.current.windowRange!.to - 1_000));
    expect(calls).toHaveLength(1);
    expect(lastOf(nowsOf(frames))).toBe(W.to - 1_000);
    expect(result.current).toMatchObject({ status: "ready", windowRange: R });
  });

  it("asks for nothing when the thumb is dragged to its left end three times", async () => {
    const { result, frames } = await opened();
    for (let i = 0; i < 3; i++) {
      await advance(200);
      act(() => result.current.seek(result.current.windowRange!.from));
      expect(result.current).toMatchObject({ clockMs: R.from, frameNowMs: R.from, windowRange: R, status: "ready" });
    }
    expect(calls).toHaveLength(1);
    expect(nowsOf(frames).slice(-3)).toEqual([R.from, R.from, R.from]);
  });

  it("spans up to the archive's edge: while loading as it stands, once loaded as it stood at the fetch, which is served", async () => {
    const { result } = setup({ t: EDGE - MIN });
    expect(result.current).toMatchObject({ status: "loading", windowRange: { from: EDGE - 15 * MIN, to: EDGE } });
    await respond(calls[0]);
    const { frames } = await subscribe(result);
    expect(result.current).toMatchObject({ status: "ready", windowRange: { from: EDGE - 15 * MIN, to: EDGE } });
    await advance(200);
    act(() => result.current.seek(EDGE));
    expect(calls).toHaveLength(1);
    expect(lastOf(nowsOf(frames))).toBe(EDGE);
  });
});

describe("a truncated window (B2, B4, B5)", () => {
  const halfAround = (c: number) => ({ from: c - 7.5 * MIN, to: c + 7.5 * MIN });

  it("serves up to the server's cut and no further: a seek past it, inside the window, clears the map there and loads the window around it", async () => {
    const { result } = setup();
    await respond(calls[0], cutAt(T + 5 * MIN));
    const { frames } = await subscribe(result);
    expect(result.current).toMatchObject({ status: "ready", truncated: true, windowRange: servable(W, T + 5 * MIN) });
    await advance(200);
    act(() => result.current.seek(T + 5 * MIN));
    expect(calls).toHaveLength(1);
    expect(lastOf(nowsOf(frames))).toBe(T + 5 * MIN);
    expect(lastOf(frames)!.aircraft.length).toBeGreaterThan(0);
    await advance(200);
    act(() => result.current.seek(T + 5 * MIN + 1));
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: T + 5 * MIN - 15 * MIN, to: T + 5 * MIN + 15 * MIN, fleet: "real" });
    expect(lastOf(frames)).toEqual(emptyAt(T + 5 * MIN + 1));
  });

  it("serves up to the last row's frame time, not its publication: the cut leaves out rows with a later frame time, so a seek one ms past it loads the window around the clock", async () => {
    const F = T + 5 * MIN;
    const cut = cutAt(F);
    const resp = { ...cut, rows: cut.rows.map((r) => (r.frame_ts_ms === F ? { ...r, solve_ts_ms: F + 800 } : r)) };
    expect(Math.max(...resp.rows.map((r) => r.solve_ts_ms))).toBe(F + 800);
    const { result } = setup();
    await respond(calls[0], resp);
    const { frames } = await subscribe(result);
    expect(result.current).toMatchObject({ status: "ready", truncated: true, windowRange: servable(W, F) });
    await advance(200);
    act(() => result.current.seek(F));
    expect(calls).toHaveLength(1);
    expect(lastOf(nowsOf(frames))).toBe(F);
    await advance(200);
    act(() => result.current.seek(F + 1));
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: F - 15 * MIN, to: F + 15 * MIN, fleet: "real" });
    expect(lastOf(frames)).toEqual(emptyAt(F + 1));
  });

  it("cut before the clock it was asked around, asks for the window around the clock on half the radius, shows the clock once that lands whole, and goes back to the span", async () => {
    const { result } = setup();
    const { frames } = await subscribe(result);
    await respond(calls[0], cutAt(T - 10_000));
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ ...halfAround(T), fleet: "real" });
    expect(result.current).toMatchObject({ status: "loading", truncated: true, windowRange: servable(halfAround(T)) });
    // Nothing past the cut was shown.
    expect(frames).toEqual([]);
    await respond(calls[1]);
    expect(result.current).toMatchObject({ status: "ready", truncated: false, windowRange: servable(halfAround(T)) });
    expect(frames).toEqual([{ ...synthesizeFrame(buildReplayIndex(KEYED.known), T), reset: true }]);
    // It landed whole, so the next load is on the span again.
    const far = T + 3 * 3_600_000;
    act(() => result.current.seek(far));
    expect(calls).toHaveLength(3);
    expect(calls[2].params).toEqual({ from: far - 15 * MIN, to: far + 15 * MIN, fleet: "real" });
  });

  it("halves again while the window around the clock is cut before it, down to two minutes, then says the instant is too dense and stops playing, asking for nothing more", async () => {
    const { result } = setup();
    act(() => result.current.play());
    for (let i = 0; i < 4; i++) await respond(calls[i], cutAt(T - 1_000));
    expect(calls.map((c) => [(c.params.from + c.params.to) / 2, (c.params.to - c.params.from) / 2])).toEqual(
      [15, 7.5, 3.75, 2].map((m) => [T, m * MIN]),
    );
    expect(result.current).toMatchObject({ status: "error", error: TOO_DENSE, playing: false, clockMs: T });
    await advance(60_000);
    expect(calls).toHaveLength(4);
    // The same window would come back cut the same: play asks for nothing, and stops at once.
    act(() => result.current.play());
    await advance(1_000);
    expect(calls).toHaveLength(4);
    expect(result.current).toMatchObject({ status: "error", error: TOO_DENSE, playing: false, clockMs: T });
  });

  it("never widens a span under the floor: a one-minute window cut before the clock is too dense at once", async () => {
    const { result } = setup({ spanMin: 1 });
    await respond(calls[0], cutAt(T - 1_000));
    expect(calls).toHaveLength(1);
    expect(result.current).toMatchObject({ status: "error", error: TOO_DENSE });
  });

  it("keeps the narrowed radius for the loads after it, the prefetch's too, until one lands whole", async () => {
    const { result } = setup();
    await respond(calls[0], cutAt(T - 1_000));
    // Narrowed, and cut again, but after the clock: it shows the clock, and the radius stays narrowed.
    await respond(calls[1], cutAt(T + 3 * MIN));
    expect(result.current).toMatchObject({ status: "ready", truncated: true, windowRange: servable(halfAround(T), T + 3 * MIN) });
    act(() => result.current.seek(T + 2 * MIN - 1_000));
    act(() => result.current.play());
    await advance(1_000);
    // A minute (the margin at 1x) before the cut, the next window is asked for, on the narrowed radius.
    expect(result.current.clockMs).toBe(T + 2 * MIN);
    expect(calls).toHaveLength(3);
    expect(calls[2].params).toEqual({ ...halfAround(T + 2 * MIN), fleet: "real" });
    await respond(calls[2]);
    act(() => result.current.pause());
    const far = T + 3 * 3_600_000;
    act(() => result.current.seek(far));
    expect(calls[3].params).toEqual({ from: far - 15 * MIN, to: far + 15 * MIN, fleet: "real" });
  });

  it("narrows rather than asking for the loaded window again when play reaches a cut on the clock's own second", async () => {
    const { result } = setup();
    await respond(calls[0], cutAt(T));
    expect(result.current).toMatchObject({ status: "ready", clockMs: T, windowRange: servable(W, T) });
    act(() => result.current.play());
    await advance(250);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ ...halfAround(T), fleet: "real" });
    expect(result.current).toMatchObject({ clockMs: T, playing: true, status: "loading" });
  });

  it("plays to a cut, asking for the next window a minute before it, holds on the cut with nothing from past it, and swaps without a reset", async () => {
    const { result } = setup();
    await respond(calls[0], cutAt(T + 5 * MIN));
    const { frames } = await subscribe(result);
    act(() => result.current.seek(T + 4 * MIN - 1_000));
    act(() => result.current.play());
    await advance(1_000);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: T + 4 * MIN - 15 * MIN, to: T + 4 * MIN + 15 * MIN, fleet: "real" });
    frames.length = 0;
    await advance(62_000);
    // Unanswered, the clock plays on to the cut, the last instant the window serves, and holds there.
    expect(result.current).toMatchObject({ clockMs: T + 5 * MIN, playing: true, status: "loading" });
    expect(Math.max(...nowsOf(frames))).toBe(T + 5 * MIN);
    expect(lastOf(nowsOf(frames))).toBe(T + 5 * MIN);
    expect(calls).toHaveLength(2);
    await respond(calls[1]);
    await advance(500);
    expect(result.current).toMatchObject({ clockMs: T + 5 * MIN + 500, playing: true, status: "ready" });
    expect(resetsOf(frames).every((r) => r === false)).toBe(true);
  });

  it("at a cut within a flush period of the archive's edge, asks for the window around the clock instead of stopping as at the edge", async () => {
    const { result } = setup({ t: EDGE - 30_000 });
    await respond(calls[0], cutAt(EDGE - 20_000));
    act(() => result.current.play());
    await advance(10_000);
    expect(result.current).toMatchObject({ clockMs: EDGE - 20_000, playing: true });
    expect(calls).toHaveLength(1);
    await advance(250);
    // A window ended by the lag stops here (G1), the archive holding only 70 s
    // more; a later fetch never moves a cut, so the next window is asked for.
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: EDGE - 20_000 - 15 * MIN, to: EDGE - 20_000 + 15 * MIN, fleet: "real" });
    expect(result.current).toMatchObject({ clockMs: EDGE - 20_000, playing: true, status: "loading" });
  });
});

describe("a legacy track across windows (B7)", () => {
  /** One legacy dark target (no track_key, no adsb_hex, no lane) solved every 2 s from T - 30 min to T + 4 h, east at 100 m/s. */
  const LEGACY_RUN: TrackRow[] = [];
  for (let f = T - 30 * MIN; f < T + 240 * MIN; f += 2_000) {
    const s = (f - (T - 30 * MIN)) / 1000;
    LEGACY_RUN.push({
      ...KEYED.known.rows[0],
      frame_ts_ms: f, solve_ts_ms: f + 800, track_key: null, lane: null, adsb_hex: null,
      lat: 34.8, lon: -82.6 + (s * 100) / (111_320 * Math.cos((34.8 * Math.PI) / 180)),
    });
  }
  /** What the API returns for [from, to): the rows solved inside it. */
  const legacyWindow = (from: number, to: number): TracksResponse => {
    const rows = LEGACY_RUN.filter((r) => r.solve_ts_ms >= from && r.solve_ts_ms < to);
    return { from, to, fleet: "real", count: rows.length, truncated: false, rows };
  };
  /** The hex a window around `c` mints for the target on its own. */
  const mintedAround = (c: number, radius = 15 * MIN) => multinodeHexFromKey(mintedDarkKey(legacyWindow(c - radius, c + radius).rows[0]));
  const hexesOf = (frames: InjectedFrame[]) => [...new Set(frames.flatMap((f) => f.aircraft.map((a) => a.hex)))];

  beforeEach(() => {
    archiveTracks.mockImplementation((p) => Promise.resolve(legacyWindow(p.from, p.to)));
  });

  it("keeps the hex the first window gave it when playback swaps to the next", async () => {
    const { result } = setup();
    await act(async () => {});
    const { frames } = await subscribe(result);
    act(() => {
      result.current.setRate(20);
      result.current.play();
    });
    await advance(40_000);
    // The prefetch at T + 600 s (five minutes before the end at 20x) landed and took over.
    expect(centresAsked()).toEqual([T, T + 600_000]);
    expect(result.current).toMatchObject({ clockMs: T + 800_000, windowRange: servable({ from: T + 600_000 - 15 * MIN, to: T + 600_000 + 15 * MIN }) });
    expect(frames.every((f) => f.aircraft.length === 1)).toBe(true);
    expect(hexesOf(frames)).toEqual([mintedAround(T)]);
    expect(mintedAround(T + 600_000)).not.toBe(mintedAround(T));
  });

  it("keeps it through a reload on another span", async () => {
    const { result, rerender } = setup();
    await act(async () => {});
    const { frames } = await subscribe(result);
    rerender({ spanMin: 10 });
    await act(async () => {});
    expect(centresAsked()).toEqual([T, T]);
    expect(result.current).toMatchObject({ status: "ready", windowRange: servable({ from: T - 10 * MIN, to: T + 10 * MIN }) });
    act(() => result.current.seek(T + 1_000));
    expect(frames.length).toBeGreaterThanOrEqual(3);
    expect(hexesOf(frames)).toEqual([mintedAround(T)]);
  });

  it("mints a new one after a far seek, which shares no row with the window before", async () => {
    const { result } = setup();
    await act(async () => {});
    const { frames } = await subscribe(result);
    const far = T + 3 * 3_600_000;
    act(() => result.current.seek(far));
    await act(async () => {});
    expect(lastOf(frames)).toMatchObject({ now: far / 1000 });
    expect(lastOf(frames)!.aircraft.map((a) => a.hex)).toEqual([mintedAround(far)]);
    expect(mintedAround(far)).not.toBe(mintedAround(T));
  });
});

describe("errors (R4)", () => {
  it.each([
    [new HttpError(422, "Unprocessable Entity", { detail: "the window is capped at 60 minutes" }), { kind: "invalid", status: 422, message: "the window is capped at 60 minutes" }],
    [new HttpError(422, "Unprocessable Entity", { detail: [{ msg: "bad" }] }), { kind: "invalid", status: 422, message: "422 Unprocessable Entity" }],
    [new HttpError(400, "Bad Request", { detail: "to must be after from" }), { kind: "invalid", status: 400, message: "to must be after from" }],
    [new HttpError(503, "Service Unavailable", null), { kind: "unavailable", status: 503, message: "503 Service Unavailable" }],
    [new HttpError(500, "", null), { kind: "other", status: 500, message: "HTTP 500" }],
    [new UnauthorizedError(), { kind: "other", status: 401, message: "401 Unauthorized" }],
    [new DOMException("No response within 30000 ms", "TimeoutError"), { kind: "network", message: "No response within 30000 ms" }],
    [new TypeError("Failed to fetch"), { kind: "network", message: "Failed to fetch" }],
    [new Error("boom"), { kind: "network", message: "boom" }],
  ])("reads %s as %o", async (err, expected) => {
    const { result } = setup();
    await fail(calls[0], err);
    expect(result.current).toMatchObject({ status: "error", playing: false, windowRange: R });
    expect(result.current.error).toEqual(expected);
  });

  it("ignores an abort", async () => {
    const { result } = setup();
    await fail(calls[0], new DOMException("The operation was aborted.", "AbortError"));
    expect(result.current).toMatchObject({ status: "loading", error: null });
  });

  it("stops playback, and keeps the loaded window for the instants it serves", async () => {
    const { result, frames } = await opened();
    act(() => result.current.play());
    await advance(500);
    act(() => result.current.seek(T - 2 * 3_600_000));
    expect(result.current.playing).toBe(true);
    await fail(calls[1], new HttpError(503, "Service Unavailable", null));
    expect(result.current).toMatchObject({ status: "error", playing: false, error: { kind: "unavailable" } });
    await advance(200);
    act(() => result.current.seek(T + 5_000));
    expect(calls).toHaveLength(2);
    expect(lastOf(nowsOf(frames))).toBe(T + 5_000);
    expect(result.current).toMatchObject({ status: "ready", error: null, windowRange: R, count: KEYED.known.count });
  });

  it("retries the window that failed", async () => {
    const { result } = setup();
    await fail(calls[0], new HttpError(503, "Service Unavailable", null));
    act(() => result.current.retry());
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: W.from, to: W.to, fleet: "real" });
    expect(result.current).toMatchObject({ status: "loading", error: null, windowRange: R });
    await respond(calls[1]);
    expect(result.current).toMatchObject({ status: "ready", trackCount: 2 });
  });
});

describe("the URL (R9)", () => {
  it("follows a pause, a landed seek and a step, and never names the span", async () => {
    const { result } = await opened();
    act(() => result.current.play());
    await advance(1_000);
    act(() => result.current.pause());
    expect(hashWrites.mock.calls).toEqual([[T + 1_000]]);
    act(() => result.current.seek(T + 30_000));
    expect(lastOf(hashWrites.mock.calls)).toEqual([T + 30_000]);
    await advance(200);
    act(() => result.current.step(-1));
    expect(lastOf(hashWrites.mock.calls)).toEqual([T + 29_000]);
    expect(hashWrites).toHaveBeenCalledTimes(3);
    // A pause while paused is no pause.
    act(() => result.current.pause());
    expect(hashWrites).toHaveBeenCalledTimes(3);
  });

  it("follows playback at most once per 3 s of wall time", async () => {
    const { result } = await opened();
    act(() => {
      result.current.setRate(20);
      result.current.play();
    });
    await advance(2_750);
    expect(hashWrites).not.toHaveBeenCalled();
    await advance(250);
    expect(hashWrites.mock.calls).toEqual([[T + 60_000]]);
    await advance(7_000);
    expect(hashWrites.mock.calls).toEqual([[T + 60_000], [T + 120_000], [T + 180_000]]);
  });

  it("is left alone by window loads, span changes and new links", async () => {
    const { rerender } = setup();
    await respond(calls[0]);
    rerender({ spanMin: 5 });
    await respond(calls[1]);
    rerender({ rev: 1, t: T + 60_000 });
    rerender({ rev: 2, t: T - 6 * 3_600_000 });
    await respond(calls[2]);
    await advance(5_000);
    expect(hashWrites).not.toHaveBeenCalled();
  });

  it("is never written once the replay is inactive", async () => {
    const { result, rerender } = await opened();
    act(() => result.current.play());
    act(() => result.current.seek(T + 10_000));
    act(() => result.current.seek(T + 20_000));
    const written = hashWrites.mock.calls.length;
    rerender({ active: false });
    await advance(10_000);
    expect(hashWrites).toHaveBeenCalledTimes(written);
  });
});

describe("leaving archive mode (R1)", () => {
  it("drops the source, aborts the request out, clears its timers and ignores the late answer", async () => {
    const { result, rerender, frames } = await opened();
    act(() => result.current.play());
    act(() => result.current.seek(T - 2 * 3_600_000));
    act(() => result.current.seek(T - 3 * 3_600_000));
    const out = calls[1];
    const before = frames.length;
    rerender({ active: false });
    expect(out.signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(result.current).toMatchObject({
      source: null,
      playing: false,
      status: "idle",
      windowRange: null,
      error: null,
      frameNowMs: null,
      count: 0,
      trackCount: 0,
      truncated: false,
    });
    await respond(out);
    await advance(5_000);
    expect(result.current).toMatchObject({ source: null, status: "idle", windowRange: null });
    expect(frames).toHaveLength(before);
    expect(calls).toHaveLength(2);
  });

  it("opens a new session, with a new source, on the way back", async () => {
    const { result, rerender } = await opened();
    const first = result.current.source;
    rerender({ active: false });
    rerender({ active: true, rev: 1 });
    expect(result.current.source).not.toBeNull();
    expect(result.current.source).not.toBe(first);
    expect(calls).toHaveLength(2);
    expect(result.current).toMatchObject({ status: "loading", windowRange: R, clockMs: T });
  });

  it("keeps one source for the whole session", async () => {
    const { result, rerender } = await opened();
    const source = result.current.source;
    act(() => result.current.play());
    await advance(1_000);
    rerender({ rev: 1, t: T - 6 * 3_600_000, spanMin: 20 });
    expect(result.current.source).toBe(source);
  });
});

describe("with the map's feed", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "WebSocket",
      class {
        constructor() {
          throw new Error("a replay opens no socket");
        }
      },
    );
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  });

  it("puts the replayed aircraft on the map at t, and starts over on a jump back", async () => {
    const { result } = renderHook(() => {
      const replay = useArchiveReplay({ active: true, t: T, spanMin: 15, rev: 0 });
      const feed = useAircraftFeed(false, "real", replay.source);
      return { replay, feed };
    });
    await respond(calls[0]);
    const expected = synthesizeFrame(buildReplayIndex(KEYED.known), T);
    expect(result.current.feed.aircraft.map((a: { hex: string }) => a.hex).sort()).toEqual(
      expected.aircraft.map((a) => a.hex).sort(),
    );
    expect(result.current.replay.frameNowMs).toBe(T);
    const epoch = result.current.feed.resetEpoch;

    act(() => result.current.replay.seek(T - 10 * MIN));
    expect(result.current.feed.resetEpoch).toBe(epoch + 1);
    expect(result.current.feed.aircraft).toEqual([]);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("takes the aircraft off the map at once on a jump to an instant that isn't loaded yet (G2)", async () => {
    const { result } = renderHook(() => {
      const replay = useArchiveReplay({ active: true, t: T, spanMin: 15, rev: 0 });
      const feed = useAircraftFeed(false, "real", replay.source);
      return { replay, feed };
    });
    await respond(calls[0]);
    expect(result.current.feed.aircraft).toHaveLength(2);
    const epoch = result.current.feed.resetEpoch;

    const far = T + 3 * 3_600_000;
    act(() => result.current.replay.seek(far));
    expect(calls).toHaveLength(2);
    expect(result.current.feed.aircraft).toEqual([]);
    expect(result.current.feed.resetEpoch).toBe(epoch + 1);
    expect(result.current.replay).toMatchObject({ clockMs: far, frameNowMs: far, status: "loading" });

    await respond(calls[1], shifted(KEYED.known, far - T));
    expect(result.current.feed.aircraft).toHaveLength(2);
    expect(result.current.feed.resetEpoch).toBe(epoch + 1);
  });
});
