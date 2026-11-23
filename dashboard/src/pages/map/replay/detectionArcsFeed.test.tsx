import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAircraftFeed, type FeedSource, type InjectedFrame } from "../hooks";
import { DETECTIONS, DETECTION_NODES } from "./__fixtures__/detections";
import { buildDetectionWindow, detectionArcs } from "./detectionArcs";

vi.mock("../../../utils/domains", () => ({ usesRealOnlyFeed: false }));
vi.mock("../../../context/AuthContext", () => ({ useAuth: () => ({ user: null, loading: false }) }));

/** The wall clock: five days after the replayed detections. */
const WALL_MS = Date.UTC(2026, 9, 23, 12, 0, 0);

function manualSource() {
  let handler: ((frame: InjectedFrame) => void) | null = null;
  const source: FeedSource = {
    subscribe: (onFrame) => {
      handler = onFrame;
      return () => {
        handler = null;
      };
    },
  };
  return { source, emit: (frame: InjectedFrame) => act(() => handler!(frame)) };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(WALL_MS);
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("a replayed detection arc in the map's afterglow buffer", () => {
  it("ages from its own frame time on the replay clock: held 2 s on, gone past 5 s", () => {
    const resp = DETECTIONS.tagged;
    const window = buildDetectionWindow(resp, resp.from, resp.to);
    const T = resp.rows[0].frame_ts_ms;
    // The four nodes that saw the first solve, handed over a second after it,
    // as a window that lands late hands them.
    const arcs = detectionArcs([{ window, lo: T - 1 }], T, [], DETECTION_NODES);
    expect(arcs.map((a) => a.node_ref)).toEqual(["nde0rho4ug5mfn9", "nde565zm6h3qldx", "ndekrn63vdrrp2f", "ndeysv8z5xpyd7f"]);

    const feed = manualSource();
    const { result } = renderHook(() => useAircraftFeed(false, "real", feed.source));
    const anchors = () => Object.values(result.current.arcsBufferRef.current).map((e) => (e as { ts: number }).ts);

    feed.emit({ now: (T + 1_000) / 1000, aircraft: [], detection_arcs: arcs, reset: true });
    expect(anchors()).toEqual([T, T, T, T]);

    feed.emit({ now: (T + 2_000) / 1000, aircraft: [], detection_arcs: [] });
    expect(anchors()).toEqual([T, T, T, T]);
    feed.emit({ now: (T + 5_000) / 1000, aircraft: [], detection_arcs: [] });
    expect(anchors()).toHaveLength(4);
    feed.emit({ now: (T + 5_001) / 1000, aircraft: [], detection_arcs: [] });
    expect(anchors()).toEqual([]);
    expect(WALL_MS - T).toBeGreaterThan(4 * 86_400_000);
  });
});
