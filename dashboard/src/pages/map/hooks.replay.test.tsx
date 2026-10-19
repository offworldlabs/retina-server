import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAircraftFeed, type FeedSource, type InjectedFrame } from "./hooks";
import type { FeedMode } from "./feedMode";

vi.mock("../../utils/domains", () => ({ usesRealOnlyFeed: false }));
vi.mock("../../context/AuthContext", () => ({ useAuth: () => ({ user: null, loading: false }) }));

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: ((event?: { code: number }) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { Socket.instances.push(this); }
  open() { this.readyState = Socket.OPEN; this.onopen?.(); }
  close() { this.readyState = 3; this.onclose?.(); }
  message(data: object) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

// The wall clock, and a replayed instant sixteen hours before it.
const WALL_MS = Date.UTC(2026, 9, 19, 12, 0, 0);
const T0 = Date.UTC(2026, 9, 18, 20, 0, 0) / 1000;

/** A source the test drives by hand. It keeps every handler it was given, so a
 *  test can also deliver a frame to one the hook has let go of. */
function manualSource() {
  const handlers: ((frame: InjectedFrame) => void)[] = [];
  const unsubscribe = vi.fn();
  const source: FeedSource = {
    subscribe: vi.fn((onFrame: (frame: InjectedFrame) => void) => {
      handlers.push(onFrame);
      return unsubscribe;
    }),
  };
  return {
    source,
    unsubscribe,
    /** The handler of the latest subscription. */
    latest: () => handlers[handlers.length - 1],
    /** Delivers `frame` as the source would on a tick. */
    emit: (frame: InjectedFrame) => act(() => handlers[handlers.length - 1](frame)),
  };
}

/** A replayed multinode solve at `nowS`, one step on from the second before. */
const solve = (hex: string, nowS: number, lat = 51) => ({
  hex,
  lat,
  lon: -1,
  alt_baro: 9000,
  contributing_node_refs: ["node-a"],
  recent_positions: [[lat - 0.01, -1, 9000, nowS - 1], [lat, -1, 9000, nowS]],
});

const frame = (nowS: number, aircraft: object[], extra: Partial<InjectedFrame> = {}): InjectedFrame => ({
  now: nowS,
  aircraft,
  ...extra,
});

const arc = (nodeRef: string, delayUs: number) => ({
  node_ref: nodeRef,
  ambiguity_arc: [[51, -1], [52, -1]],
  delay_us: delayUs,
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(WALL_MS);
  Socket.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("an injected frame source", () => {
  it.each([
    ["real", false],
    ["all", false],
    ["synthetic", false],
    ["real", true],
  ] as [FeedMode, boolean][])("replaces the socket, the poll and the watchdog (%s, owner %s)", async (mode, owner) => {
    const feed = manualSource();
    const { result } = renderHook(() => useAircraftFeed(owner, mode, feed.source));
    expect(feed.source.subscribe).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(Socket.instances).toEqual([]);
    expect(fetch).not.toHaveBeenCalled();
    expect(result.current.connected).toBe(false);
  });

  it("ingests a frame on the frame's clock, not the wall clock", () => {
    const feed = manualSource();
    const { result } = renderHook(() => useAircraftFeed(false, "real", feed.source));
    const tracked = solve("mn1", T0);
    // No recent_positions: the hook lays the trail point itself.
    const bare = { hex: "bare", lat: 52, lon: 0, alt_baro: 1000, ...arc("node-b", 12) };
    feed.emit(frame(T0, [tracked, bare]));

    expect(result.current.aircraft).toEqual([tracked, bare]);
    expect(result.current.trailsRef.current["mn1"]).toEqual(tracked.recent_positions);
    expect(result.current.trailsRef.current["bare"]).toEqual([[52, 0, 1000, T0]]);
    expect(result.current.detectionsRef.current).toEqual({
      "mn1|node-a": T0 * 1000,
      "bare|node-b": T0 * 1000,
    });
    expect(Object.values(result.current.arcsBufferRef.current).map((e: { ts: number }) => e.ts)).toEqual([T0 * 1000]);
    expect(Date.now()).toBe(WALL_MS);
  });

  it("prunes replay trails on the frame's clock, not the wall clock", () => {
    const feed = manualSource();
    const { result } = renderHook(() => useAircraftFeed(false, "real", feed.source));
    const trails = () => result.current.trailsRef.current;
    const lastTs = (hex: string) => trails()[hex]?.at(-1)?.[3];
    // "kept" is in every frame; "gone" leaves after T0 + 30. A prune runs on
    // every 60th frame: frames 59, 119 … 299 fall before T0 + 330, and 359 is
    // the first past it.
    const play = (from: number, to: number) => {
      for (let i = from; i <= to; i++) {
        const lat = 51 + i * 0.01;
        feed.emit(frame(T0 + i, i <= 30 ? [solve("kept", T0 + i, lat), solve("gone", T0 + i, lat)] : [solve("kept", T0 + i, lat)]));
      }
    };

    // Past the first prune: everything is sixteen hours older than the wall clock.
    play(0, 60);
    expect(Object.keys(trails()).sort()).toEqual(["gone", "kept"]);
    expect(trails()["kept"]).toHaveLength(62);
    expect(lastTs("kept")).toBe(T0 + 60);
    expect(lastTs("gone")).toBe(T0 + 30);

    // The prune at T0 + 299 is still inside five minutes of T0 + 30.
    play(61, 358);
    expect(lastTs("gone")).toBe(T0 + 30);

    // The prune at T0 + 359 is not.
    play(359, 359);
    expect(Object.keys(trails())).toEqual(["kept"]);
    expect(lastTs("kept")).toBe(T0 + 359);
  });

  it("empties every store before a seek frame, and counts the reset", () => {
    const feed = manualSource();
    const { result } = renderHook(() => useAircraftFeed(false, "real", feed.source));
    feed.emit(frame(T0, [{ ...solve("before", T0), ...arc("node-a", 10) }], {
      anomaly_hexes: ["before"],
      ground_truth: { before: { lat: 51, lon: -1 } },
      ground_truth_meta: { source: "replay" },
    }));
    expect(Object.keys(result.current.arcsBufferRef.current)).toHaveLength(1);
    const epoch = result.current.resetEpoch;

    // A seek ten minutes back: on the earlier clock nothing of the later
    // instant would age out by itself.
    const back = T0 - 600;
    feed.emit(frame(back, [{ ...solve("after", back), ...arc("node-b", 20) }], { reset: true }));

    expect(result.current.resetEpoch).toBe(epoch + 1);
    expect(result.current.aircraft.map((a) => a.hex)).toEqual(["after"]);
    expect(Object.keys(result.current.trailsRef.current)).toEqual(["after"]);
    expect(Object.values(result.current.arcsBufferRef.current).map((e: { hex: string }) => e.hex)).toEqual(["after"]);
    expect(Object.keys(result.current.detectionsRef.current).sort()).toEqual(["after|node-a", "after|node-b"]);
    expect(result.current.anomalyHexesRef.current.size).toBe(0);
    expect(result.current.groundTruthRef.current).toEqual({});
    expect(result.current.groundTruthMetaRef.current).toEqual({});

    // An ordinary frame is no reset.
    feed.emit(frame(back + 1, [solve("after", back + 1, 51.01)]));
    expect(result.current.resetEpoch).toBe(epoch + 1);
  });

  it("counts every reset of the stores in resetEpoch", () => {
    const a = manualSource();
    const b = manualSource();
    const { result, rerender } = renderHook(
      ({ owner, source }: { owner: boolean; source: FeedSource | null }) => useAircraftFeed(owner, "real", source),
      { initialProps: { owner: false, source: null } },
    );
    // The first scope's own reset.
    expect(result.current.resetEpoch).toBe(1);
    act(() => {
      Socket.instances[0].open();
      Socket.instances[0].message({ aircraft: [{ hex: "live", lat: 51, lon: -1 }] });
    });
    expect(result.current.resetEpoch).toBe(1);
    rerender({ owner: true, source: null });
    expect(result.current.resetEpoch).toBe(2);
    rerender({ owner: true, source: a.source });
    expect(result.current.resetEpoch).toBe(3);
    a.emit(frame(T0, [solve("mn1", T0)]));
    expect(result.current.resetEpoch).toBe(3);
    a.emit(frame(T0 - 60, [solve("mn1", T0 - 60)], { reset: true }));
    expect(result.current.resetEpoch).toBe(4);
    rerender({ owner: true, source: b.source });
    expect(result.current.resetEpoch).toBe(5);
    rerender({ owner: true, source: null });
    expect(result.current.resetEpoch).toBe(6);
  });

  it.each([
    ["real", false],
    ["all", false],
    ["real", true],
  ] as [FeedMode, boolean][])("goes live as a fresh scope once the source is removed (%s, owner %s)", async (mode, owner) => {
    // The socket the live hook opens for this page.
    const live = renderHook(() => useAircraftFeed(owner, mode));
    const liveUrl = Socket.instances[0].url;
    live.unmount();
    Socket.instances = [];
    vi.mocked(fetch).mockClear();

    const feed = manualSource();
    const { result, rerender } = renderHook(
      ({ source }: { source: FeedSource | null }) => useAircraftFeed(owner, mode, source),
      { initialProps: { source: feed.source } },
    );
    feed.emit(frame(T0, [{ ...solve("replayed", T0), ...arc("node-a", 10) }], {
      anomaly_hexes: ["replayed"],
      ground_truth: { replayed: { lat: 51, lon: -1 } },
    }));
    expect(result.current.aircraft).toHaveLength(1);

    rerender({ source: null });
    expect(feed.unsubscribe).toHaveBeenCalledTimes(1);
    expect(Socket.instances).toHaveLength(1);
    expect(Socket.instances[0].url).toBe(liveUrl);
    expect(result.current.connected).toBe(false);
    expect(result.current.aircraft).toEqual([]);
    for (const name of ["trailsRef", "groundTruthRef", "groundTruthMetaRef", "arcsBufferRef", "detectionsRef"] as const) {
      expect(result.current[name].current).toEqual({});
    }
    expect(result.current.anomalyHexesRef.current.size).toBe(0);
    // The public page polls at once, as a fresh live scope does; the owner's never.
    expect(fetch).toHaveBeenCalledTimes(owner ? 0 : 1);

    // The socket path is as it always was: the wall clock, and the pause history.
    act(() => {
      Socket.instances[0].open();
      Socket.instances[0].message({ aircraft: [{ hex: "live", lat: 51, lon: -1, node_ref: "node-a" }] });
    });
    expect(result.current.connected).toBe(true);
    expect(result.current.aircraft.map((a) => a.hex)).toEqual(["live"]);
    expect(result.current.detectionsRef.current).toEqual({ "live|node-a": WALL_MS });
    expect(result.current.historyRef.current).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(Socket.instances).toHaveLength(1);
  });

  it("lets go of the source on unmount, and drops a frame it delivers late", () => {
    const feed = manualSource();
    const { result, unmount } = renderHook(() => useAircraftFeed(false, "real", feed.source));
    const late = feed.latest();
    const { trailsRef, detectionsRef } = result.current;
    unmount();
    expect(feed.unsubscribe).toHaveBeenCalledTimes(1);
    act(() => late(frame(T0, [solve("late", T0)])));
    expect(trailsRef.current).toEqual({});
    expect(detectionsRef.current).toEqual({});
  });

  it("takes a different source as a new scope, and drops the old one's late frames", () => {
    const a = manualSource();
    const b = manualSource();
    const { result, rerender } = renderHook(
      ({ source }: { source: FeedSource }) => useAircraftFeed(false, "real", source),
      { initialProps: { source: a.source } },
    );
    a.emit(frame(T0, [solve("from-a", T0)]));
    const lateA = a.latest();
    // The same source again is the same scope.
    rerender({ source: a.source });
    expect(a.source.subscribe).toHaveBeenCalledTimes(1);
    expect(a.unsubscribe).not.toHaveBeenCalled();

    rerender({ source: b.source });
    expect(a.unsubscribe).toHaveBeenCalledTimes(1);
    expect(b.source.subscribe).toHaveBeenCalledTimes(1);
    expect(result.current.aircraft).toEqual([]);
    expect(result.current.trailsRef.current).toEqual({});

    act(() => lateA(frame(T0 + 1, [solve("from-a", T0 + 1, 51.01)])));
    expect(result.current.aircraft).toEqual([]);
    expect(result.current.trailsRef.current).toEqual({});

    b.emit(frame(T0 + 1, [solve("from-b", T0 + 1)]));
    expect(result.current.aircraft.map((ac) => ac.hex)).toEqual(["from-b"]);
    expect(Object.keys(result.current.trailsRef.current)).toEqual(["from-b"]);
    expect(Socket.instances).toEqual([]);
  });

  it("keeps injected frames out of the pause history", () => {
    const feed = manualSource();
    const { result } = renderHook(() => useAircraftFeed(false, "real", feed.source));
    for (let i = 0; i < 5; i++) feed.emit(frame(T0 + i, [solve("mn1", T0 + i, 51 + i * 0.01)]));
    expect(result.current.aircraft).toHaveLength(1);
    expect(result.current.trailsRef.current["mn1"]).toHaveLength(6);
    expect(result.current.historyRef.current).toEqual([]);
  });

  it("holds the shown aircraft while paused, as the socket path does", () => {
    const feed = manualSource();
    const { result } = renderHook(() => useAircraftFeed(false, "real", feed.source));
    feed.emit(frame(T0, [solve("first", T0)]));
    act(() => result.current.setPaused(true));
    feed.emit(frame(T0 + 1, [solve("second", T0 + 1)]));
    expect(result.current.aircraft.map((ac) => ac.hex)).toEqual(["first"]);
    expect(Object.keys(result.current.trailsRef.current).sort()).toEqual(["first", "second"]);
  });
});
