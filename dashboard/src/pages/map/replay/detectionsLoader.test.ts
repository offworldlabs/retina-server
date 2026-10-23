import { HttpError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../../api/client";
import { DETECTIONS, DETECTIONS_CLOCK_MS, DETECTION_NODES } from "./__fixtures__/detections";
import type { DetectionWindow } from "./detectionArcs";
import { DetectionsLoader } from "./detectionsLoader";
import type { DetectionsResponse } from "./types";

vi.mock("../../../api/client", () => ({ api: { archiveDetections: vi.fn() } }));

const at = (h: number, m: number, s = 0, ms = 0) => Date.UTC(2026, 9, 18, h, m, s, ms);

interface Call {
  params: { from: number; to: number; fleet?: string; bbox?: unknown };
  signal: AbortSignal;
  resolve(resp: DetectionsResponse): void;
  reject(err: unknown): void;
}
let calls: Call[] = [];
const archiveDetections = vi.mocked(api.archiveDetections);

/** A loader on a session whose clock and rate the test sets, turned on. */
function loader(clock = DETECTIONS_CLOCK_MS, rate = 1) {
  const host = { now: clock, rate, landed: [] as (DetectionWindow | null)[] };
  const l = new DetectionsLoader({
    clock: () => host.now,
    rate: () => host.rate,
    changed: (w) => host.landed.push(w),
  });
  l.enable();
  return { l, host };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

async function respond(call: Call, resp: DetectionsResponse) {
  call.resolve(resp);
  await settle();
}

beforeEach(() => {
  calls = [];
  archiveDetections.mockReset();
  archiveDetections.mockImplementation(
    (params, opts) =>
      new Promise<DetectionsResponse>((resolve, reject) => {
        const signal = opts!.signal!;
        signal.addEventListener("abort", () => reject(signal.reason));
        calls.push({ params, signal, resolve, reject });
      }),
  );
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the detections loader", () => {
  it("asks for ten minutes of the real fleet from a whole second before the clock's afterglow, and no bbox", () => {
    const { l } = loader();
    expect(calls).toHaveLength(0);
    l.place(DETECTIONS_CLOCK_MS, 1, true);
    expect(calls.map((c) => c.params)).toEqual([{ from: at(19, 57), to: at(20, 7), fleet: "real" }]);
    expect(l.state(DETECTIONS_CLOCK_MS)).toEqual({ status: "loading", local: true });
  });

  it.each([
    [1, at(20, 6)],
    [5, at(20, 5, 45)],
    [20, at(20, 2)],
  ])("at %ix asks for the next ten minutes once the clock is the margin before the held window ends", async (rate, due) => {
    const { l, host } = loader(DETECTIONS_CLOCK_MS, rate);
    l.place(DETECTIONS_CLOCK_MS, rate, true);
    await respond(calls[0], DETECTIONS.tagged);
    expect(l.state(DETECTIONS_CLOCK_MS)).toEqual({ status: "ready", local: true });
    host.now = due - 1;
    l.place(due - 1, rate, false);
    expect(calls).toHaveLength(1);
    host.now = due;
    l.place(due, rate, false);
    expect(calls.map((c) => c.params)).toEqual([
      { from: at(19, 57), to: at(20, 7), fleet: "real" },
      { from: at(20, 7), to: at(20, 17), fleet: "real" },
    ]);
    // One out at a time.
    l.place(due + 250, rate, false);
    expect(calls).toHaveLength(2);
  });

  it("drops the windows and the request out on a seek away from them, and asks for the window at the new clock", async () => {
    const { l, host } = loader();
    l.place(DETECTIONS_CLOCK_MS, 1, true);
    await respond(calls[0], DETECTIONS.tagged);
    host.now = at(20, 6);
    l.place(at(20, 6), 1, false);
    const out = calls[1];

    // Inside the held window, or inside the one out: nothing.
    l.place(at(19, 58), 1, true);
    l.place(at(20, 10), 1, true);
    expect(calls).toHaveLength(2);
    expect(out.signal.aborted).toBe(false);

    host.now = at(21, 30, 2, 500);
    l.place(host.now, 1, true);
    expect(out.signal.aborted).toBe(true);
    expect(calls[2].params).toEqual({ from: at(21, 29, 57), to: at(21, 39, 57), fleet: "real" });
    await settle();
    // The abort is no failure, and the old window serves nothing now.
    expect(l.state(at(19, 58))).toEqual({ status: "loading", local: true });
    expect(l.arcsAt(at(19, 57, 10), true, [], DETECTION_NODES)).toEqual([]);
  });

  it("goes on from a cut answer's last row on a window as long as what it served, and back to ten minutes after a whole one", async () => {
    const clock = at(19, 45, 5);
    const last = at(19, 48, 50, 632);
    const { l, host } = loader(clock);
    l.place(clock, 1, true);
    expect(calls[0].params).toEqual({ from: at(19, 45), to: at(19, 55), fleet: "real" });
    await respond(calls[0], DETECTIONS.truncated);
    expect(calls).toHaveLength(1);
    expect(l.state(last - 1)).toEqual({ status: "ready", local: true });
    expect(l.state(last)).toEqual({ status: "loading", local: true });

    host.now = last - 60_000;
    l.place(host.now, 1, false);
    expect(calls[1].params).toEqual({ from: last, to: last + 230_000, fleet: "real" });
    await respond(calls[1], DETECTIONS.afterTruncation);

    // The three rows at the cut come once, from the window after it.
    const arcs = l.arcsAt(last + 1_000, true, [], DETECTION_NODES);
    expect(arcs.filter((a) => a.ts === last)).toHaveLength(3);
    expect(arcs.map((a) => a.ts)).toEqual(
      [...DETECTIONS.truncated.rows, ...DETECTIONS.afterTruncation.rows]
        .map((r) => r.frame_ts_ms)
        .filter((ts) => ts > last - 4_000 && ts <= last + 1_000 && ts !== last)
        .concat([last, last, last])
        .sort((a, b) => a - b),
    );

    host.now = last + 230_000 - 60_000;
    l.place(host.now, 1, false);
    expect(calls[2].params).toEqual({ from: last + 230_000, to: last + 830_000, fleet: "real" });
  });

  it("stops asking after a failure until a seek away from the windows held, or a retry", async () => {
    const { l, host } = loader();
    l.place(DETECTIONS_CLOCK_MS, 1, true);
    await respond(calls[0], DETECTIONS.tagged);
    host.now = at(20, 6);
    l.place(host.now, 1, false);
    calls[1].reject(new HttpError(503, "Service Unavailable", null));
    await settle();
    expect(host.landed[host.landed.length - 1]).toBeNull();
    expect(l.state(host.now)).toEqual({ status: "error", local: true });

    // Playback asks for nothing, nor does a seek inside the window held.
    l.place(at(20, 6, 0, 250), 1, false);
    l.place(at(20, 6, 30), 1, false);
    l.place(at(19, 59), 1, true);
    expect(calls).toHaveLength(2);

    l.retry(at(20, 6), 1);
    expect(calls[2].params).toEqual(calls[1].params);
    expect(l.state(at(20, 6)).status).toBe("ready");
    // An answer that cannot be read is a failure too.
    calls[2].resolve({ ...DETECTIONS.tagged, rows: "unreadable" } as unknown as DetectionsResponse);
    await settle();
    expect(l.state(at(20, 6)).status).toBe("error");

    l.place(at(21, 0), 1, true);
    expect(calls[3].params).toEqual({ from: at(20, 59, 55), to: at(21, 9, 55), fleet: "real" });
    expect(l.state(at(21, 0)).status).toBe("loading");
  });
});

describe("the detections loader on a window the server has not archived in full", () => {
  const CLOCK = DETECTIONS_CLOCK_MS;
  const WINDOW = { from: at(19, 57), to: at(20, 7), fleet: "real" };
  /** The fixture window's rows a frame at CLOCK shows: (CLOCK - 5 s, CLOCK]. */
  const shownAtClock = DETECTIONS.tagged.rows
    .map((r) => r.frame_ts_ms)
    .filter((ts) => ts > CLOCK - 5_000 && ts <= CLOCK);

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    // The wall time the pending fixture was answered at: the 20:00 write has not run.
    vi.setSystemTime(at(20, 0, 1));
  });

  async function answer(call: Call, resp: DetectionsResponse) {
    call.resolve(resp);
    await vi.advanceTimersByTimeAsync(0);
  }

  /** Wall time runs on to `until`, the clock placed at CLOCK on every 250 ms tick. */
  function tickUntil(l: DetectionsLoader, until: number) {
    while (Date.now() < until) {
      vi.advanceTimersByTime(Math.min(250, until - Date.now()));
      l.place(CLOCK, 1, false);
    }
  }

  it("says the arcs are pending, asks again once the archive can hold the window, then once a write period, and shows what came", async () => {
    const { l, host } = loader();
    l.place(CLOCK, 1, true);
    await answer(calls[0], DETECTIONS.pending);
    expect(l.state(CLOCK)).toEqual({ status: "pending", local: true });
    expect(l.arcsAt(CLOCK, true, [], DETECTION_NODES)).toEqual([]);

    // 21:18: the window's end, the hour's write, its ten minutes and one more.
    tickUntil(l, at(21, 18) - 1);
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(calls.map((c) => c.params)).toEqual([WINDOW, WINDOW]);
    // While it is out the pending one still serves the clock.
    expect(l.state(CLOCK).status).toBe("pending");

    // Still pending: the next ask is a write period after this answer.
    await answer(calls[1], DETECTIONS.pending);
    tickUntil(l, at(22, 18) - 1);
    expect(calls).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(calls).toHaveLength(3);
    expect(calls[2].params).toEqual(WINDOW);

    // Written: the arcs in the afterglow go to the map at once, paused.
    await answer(calls[2], DETECTIONS.tagged);
    expect(host.landed[host.landed.length - 1]).not.toBeNull();
    expect(l.state(CLOCK)).toEqual({ status: "ready", local: true });
    expect(l.arcsAt(CLOCK, false, [], DETECTION_NODES).map((a) => a.ts)).toEqual(shownAtClock);
    tickUntil(l, at(23, 30));
    expect(calls).toHaveLength(3);
  });

  it("keeps a whole answer that is not pending, however empty, and asks for it no more", async () => {
    const { l } = loader();
    l.place(CLOCK, 1, true);
    await answer(calls[0], { ...DETECTIONS.tagged, count: 0, rows: [] });
    expect(l.state(CLOCK)).toEqual({ status: "ready", local: true });
    tickUntil(l, at(23, 0));
    expect(calls).toHaveLength(1);
  });

  it("asks for nothing more once archive mode is left with a pending window held", async () => {
    const { l } = loader();
    l.place(CLOCK, 1, true);
    await answer(calls[0], DETECTIONS.pending);
    l.dispose();
    await vi.advanceTimersByTimeAsync(3 * 3_600_000);
    expect(calls).toHaveLength(1);
  });
});
