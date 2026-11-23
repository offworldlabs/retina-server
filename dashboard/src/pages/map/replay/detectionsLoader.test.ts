import { HttpError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../../api/client";
import type { NodeGeometry } from "../bistaticArc";
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
/** Wall time moves on a second and no timer fires: the next jump comes on its own, not in a drag. */
const apart = () => vi.setSystemTime(Date.now() + 1_000);

async function respond(call: Call, resp: DetectionsResponse) {
  call.resolve(resp);
  await settle();
}

/** A whole answer with no rows for the window `call` asked for. */
const empty = (call: Call): DetectionsResponse => ({
  from: call.params.from,
  to: call.params.to,
  fleet: "real",
  count: 0,
  truncated: false,
  local: true,
  pending: false,
  rows: [],
});

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

    apart();
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
    // A held window that covers the clock is still served after a later
    // window fails: its arcs are all there.
    expect(l.state(host.now)).toEqual({ status: "ready", local: true });

    // Playback asks for nothing, nor does a seek inside the window held. Once
    // play leaves that window, the failure is why nothing serves the clock.
    l.place(at(20, 6, 0, 250), 1, false);
    l.place(at(20, 6, 30), 1, false);
    apart();
    l.place(at(19, 59), 1, true);
    l.place(at(20, 7), 1, false);
    expect(calls).toHaveLength(2);
    expect(l.state(at(20, 7))).toEqual({ status: "error", local: true });

    l.retry(at(20, 6), 1);
    expect(calls[2].params).toEqual(calls[1].params);
    expect(l.state(at(20, 6)).status).toBe("ready");
    expect(l.state(at(20, 7)).status).toBe("loading");
    // An answer that cannot be read is a failure too.
    calls[2].resolve({ ...DETECTIONS.tagged, rows: "unreadable" } as unknown as DetectionsResponse);
    await settle();
    expect(l.state(at(20, 6)).status).toBe("ready");
    expect(l.state(at(20, 7)).status).toBe("error");

    apart();
    l.place(at(21, 0), 1, true);
    expect(calls[3].params).toEqual({ from: at(20, 59, 55), to: at(21, 9, 55), fleet: "real" });
    expect(l.state(at(21, 0)).status).toBe("loading");
  });
});

describe("the detections loader on a seek back from the windows held", () => {
  it.each([
    ["answered at once", 0],
    ["each answered ten seeks after it is asked for", 10],
  ])(
    "asks only for the stretches the clock enters: a drag back across 29 minutes, a seek every 70 s, asks for three windows, %s",
    async (_, lag) => {
      const { l, host } = loader(at(20, 30));
      l.place(at(20, 30), 1, true);
      await respond(calls[0], empty(calls[0]));
      const askedAt = new Map<Call, number>();
      for (let step = 1; step <= 25; step++) {
        host.now = at(20, 30) - step * 70_000;
        l.place(host.now, 1, true);
        for (const call of calls) {
          if (!askedAt.has(call)) askedAt.set(call, step);
          if (call !== calls[0] && askedAt.get(call)! + lag === step) await respond(call, empty(call));
        }
      }
      for (const call of calls.slice(1)) if (askedAt.get(call)! + lag > 25) await respond(call, empty(call));

      expect(calls.map((c) => c.params)).toEqual([
        { from: at(20, 29, 55), to: at(20, 39, 55), fleet: "real" },
        { from: at(20, 19, 55), to: at(20, 29, 55), fleet: "real" },
        { from: at(20, 9, 55), to: at(20, 19, 55), fleet: "real" },
        { from: at(19, 59, 55), to: at(20, 9, 55), fleet: "real" },
      ]);
      expect(calls.some((c) => c.signal.aborted)).toBe(false);
      expect(l.state(host.now)).toEqual({ status: "ready", local: true });
      // Two windows held: the one the drag ended in and the one after it.
      l.place(at(20, 15), 1, true);
      expect(calls).toHaveLength(4);
      apart();
      l.place(at(20, 25), 1, true);
      expect(calls[4].params).toEqual({ from: at(20, 24, 55), to: at(20, 34, 55), fleet: "real" });
    },
  );

  it("lets go of the windows held when the one before them is cut short of where they start, and plays on from its last row", async () => {
    const last = at(19, 48, 50, 632);
    const { l, host } = loader(at(19, 55, 5));
    l.place(at(19, 55, 5), 1, true);
    await respond(calls[0], empty(calls[0]));
    apart();
    host.now = at(19, 47);
    l.place(host.now, 1, true);
    expect(calls[1].params).toEqual({ from: at(19, 45), to: at(19, 55), fleet: "real" });
    await respond(calls[1], DETECTIONS.truncated);
    expect(l.state(host.now)).toEqual({ status: "ready", local: true });

    host.now = last - 60_000;
    l.place(host.now, 1, false);
    expect(calls).toHaveLength(3);
    expect(calls[2].params).toEqual({ from: last, to: last + 230_000, fleet: "real" });
    apart();
    l.place(at(19, 58), 1, true);
    expect(calls[3].params).toEqual({ from: at(19, 57, 55), to: at(20, 7, 55), fleet: "real" });
  });

  it("asks for the stretch from the last row of one cut short of the clock up to them, then for windows as long as what it served", async () => {
    const last = at(19, 48, 50, 632);
    const { l, host } = loader(at(19, 55, 5));
    l.place(at(19, 55, 5), 1, true);
    await respond(calls[0], empty(calls[0]));
    host.now = at(19, 52);
    l.place(host.now, 1, true);
    expect(calls[1].params).toEqual({ from: at(19, 45), to: at(19, 55), fleet: "real" });
    await respond(calls[1], DETECTIONS.truncated);
    expect(calls).toHaveLength(3);
    expect(calls[2].params).toEqual({ from: last, to: at(19, 55), fleet: "real" });
    await respond(calls[2], empty(calls[2]));
    expect(l.state(host.now)).toEqual({ status: "ready", local: true });

    host.now = at(19, 48);
    l.place(host.now, 1, true);
    expect(calls.map((c) => c.params).slice(3)).toEqual([{ from: last - 230_000, to: last, fleet: "real" }]);
  });

  it("starts over after a failure rather than going on from the windows held", async () => {
    const { l, host } = loader();
    l.place(DETECTIONS_CLOCK_MS, 1, true);
    await respond(calls[0], DETECTIONS.tagged);
    host.now = at(20, 6);
    l.place(host.now, 1, false);
    calls[1].reject(new HttpError(503, "Service Unavailable", null));
    await settle();

    apart();
    host.now = at(19, 52);
    l.place(host.now, 1, true);
    expect(calls[2].params).toEqual({ from: at(19, 51, 55), to: at(20, 1, 55), fleet: "real" });
    expect(l.state(host.now)).toEqual({ status: "loading", local: true });
  });
});

describe("the detections loader when the arcs cannot be built", () => {
  it("says error for the clock though a window held serves it", async () => {
    const { l } = loader();
    l.place(DETECTIONS_CLOCK_MS, 1, true);
    await respond(calls[0], DETECTIONS.tagged);
    const unreadable = new Proxy({} as Record<string, NodeGeometry>, {
      get() {
        throw new Error("registry unreadable");
      },
    });
    expect(l.arcsAt(DETECTIONS_CLOCK_MS, true, [], unreadable)).toEqual([]);
    expect(l.state(DETECTIONS_CLOCK_MS)).toEqual({ status: "error", local: true });
  });
});

describe("the detections loader on a 429 from the archive", () => {
  /** The archive's answer to a client asking too often, naming `retryAfterMs` as its wait. */
  const limited = (retryAfterMs: number | null) => new HttpError(429, "Too Many Requests", null, retryAfterMs);
  const CLOCK = DETECTIONS_CLOCK_MS;

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.setSystemTime(at(23, 0));
  });

  async function refuse(call: Call, retryAfterMs: number | null) {
    call.reject(limited(retryAfterMs));
    await vi.advanceTimersByTimeAsync(0);
  }

  it("is waited out rather than failed, then the same window is asked for again on its own once the wait is over", async () => {
    const { l, host } = loader();
    l.place(CLOCK, 1, true);
    await refuse(calls[0], 7_000);
    expect(l.state(CLOCK)).toEqual({ status: "waiting", local: true });
    expect(host.landed).toEqual([null]);
    vi.advanceTimersByTime(6_999);
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual(calls[0].params);
    expect(l.state(CLOCK)).toEqual({ status: "loading", local: true });
    calls[1].resolve(DETECTIONS.tagged);
    await vi.advanceTimersByTimeAsync(0);
    expect(l.state(CLOCK)).toEqual({ status: "ready", local: true });
  });

  it("keeps serving the window held while the next one waits, at most a minute, and asks for nothing else meanwhile", async () => {
    const { l, host } = loader();
    l.place(CLOCK, 1, true);
    calls[0].resolve(DETECTIONS.tagged);
    await vi.advanceTimersByTimeAsync(0);
    host.now = at(20, 6);
    l.place(host.now, 1, false);
    expect(calls).toHaveLength(2);
    await refuse(calls[1], 3_600_000);
    expect(l.state(host.now)).toEqual({ status: "ready", local: true });
    // Played past the window held, the clock waits with it.
    l.place(at(20, 7, 30), 1, false);
    expect(l.state(at(20, 7, 30))).toEqual({ status: "waiting", local: true });
    vi.advanceTimersByTime(59_999);
    expect(calls).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(calls).toHaveLength(3);
    expect(calls[2].params).toEqual(calls[1].params);
  });

  it("is let go of on a jump away, on the layer turned off and on leaving, leaving no timer behind and asking for nothing later", async () => {
    const { l, host } = loader();
    l.place(CLOCK, 1, true);
    const timers = vi.getTimerCount();
    await refuse(calls[0], 5_000);
    expect(vi.getTimerCount()).toBe(timers + 1);
    // A jump the window waited for covers waits with it; one away asks for its own window at once.
    l.place(CLOCK + 60_000, 1, true);
    expect(calls).toHaveLength(1);
    apart();
    host.now = at(21, 30);
    l.place(host.now, 1, true);
    expect(calls).toHaveLength(2);
    expect(vi.getTimerCount()).toBe(timers);
    vi.advanceTimersByTime(60_000);
    expect(calls).toHaveLength(2);

    await refuse(calls[1], 5_000);
    l.disable();
    expect(vi.getTimerCount()).toBe(timers);
    expect(l.state(host.now)).toEqual({ status: "off", local: true });
    vi.advanceTimersByTime(60_000);
    expect(calls).toHaveLength(2);

    l.enable();
    l.place(host.now, 1, true);
    await refuse(calls[2], 5_000);
    l.dispose();
    expect(vi.getTimerCount()).toBe(timers);
    vi.advanceTimersByTime(60_000);
    expect(calls).toHaveLength(3);
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

  it("says error for a pending window whose recheck failed, since it is not asked for again", async () => {
    const { l, host } = loader();
    l.place(CLOCK, 1, true);
    await answer(calls[0], DETECTIONS.pending);
    tickUntil(l, at(21, 18));
    expect(calls.map((c) => c.params)).toEqual([WINDOW, WINDOW]);

    // A 429 only puts the ask off: arcs may still come.
    calls[1].reject(new HttpError(429, "Too Many Requests", null, 5_000));
    await vi.advanceTimersByTimeAsync(0);
    expect(l.state(CLOCK)).toEqual({ status: "pending", local: true });
    vi.advanceTimersByTime(5_000);
    expect(calls.map((c) => c.params)).toEqual([WINDOW, WINDOW, WINDOW]);

    // Any other failure ends the asking, so no more arcs come on their own.
    calls[2].reject(new HttpError(503, "Service Unavailable", null));
    await vi.advanceTimersByTimeAsync(0);
    expect(host.landed[host.landed.length - 1]).toBeNull();
    expect(l.state(CLOCK)).toEqual({ status: "error", local: true });
    tickUntil(l, at(22, 30));
    expect(calls).toHaveLength(3);
    expect(l.state(CLOCK)).toEqual({ status: "error", local: true });

    // A retry asks for it again, and the arcs may come once more.
    l.retry(CLOCK, 1);
    expect(calls[3].params).toEqual(WINDOW);
    expect(l.state(CLOCK)).toEqual({ status: "pending", local: true });
  });

  it("says error for a pending window once a later window fails, since it is not asked for again either", async () => {
    const { l, host } = loader();
    l.place(CLOCK, 1, true);
    await answer(calls[0], DETECTIONS.pending);
    // At 20:10 play has reached the margin before the window ends: the next one goes out, and fails.
    vi.advanceTimersByTime(at(20, 10) - Date.now());
    host.now = at(20, 6);
    l.place(host.now, 1, false);
    expect(calls[1].params).toEqual({ from: at(20, 7), to: at(20, 17), fleet: "real" });
    calls[1].reject(new HttpError(503, "Service Unavailable", null));
    await vi.advanceTimersByTimeAsync(0);
    expect(l.state(host.now)).toEqual({ status: "error", local: true });
    // Past when the archive holds it in full, it is not asked for.
    await vi.advanceTimersByTimeAsync(2 * 3_600_000);
    expect(calls).toHaveLength(2);
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

describe("the detections loader while the scrubber is dragged", () => {
  const WINDOW = { from: at(19, 57), to: at(20, 7), fleet: "real" };

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.setSystemTime(at(23, 0));
  });

  async function answer(call: Call, resp = empty(call)) {
    call.resolve(resp);
    await vi.advanceTimersByTimeAsync(0);
  }

  async function refuse(call: Call) {
    call.reject(new HttpError(429, "Too Many Requests", null, 5_000));
    await vi.advanceTimersByTimeAsync(0);
  }

  it("asks for two windows in a minute of sweeps across half an hour, a sweep each 500 ms: one as the drag starts, one where it settles", async () => {
    // The session lands a dragged seek every 200 ms: at 19:45, 19:57, 20:09, 20:09, 19:57, and round again.
    const landed = [at(19, 45), at(19, 57), at(20, 9), at(20, 9), at(19, 57)];
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    await answer(calls[0]);
    vi.advanceTimersByTime(1_000);
    let answered = 1;
    for (let seek = 0; seek < 300; seek++) {
      host.now = landed[seek % 5];
      l.place(host.now, 1, true);
      vi.advanceTimersByTime(200);
      while (answered < calls.length) await answer(calls[answered++]);
    }
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(19, 44, 55), to: at(19, 54, 55), fleet: "real" }]);
    vi.advanceTimersByTime(99);
    expect(calls).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(calls.map((c) => c.params).slice(2)).toEqual([{ from: at(19, 56, 55), to: at(20, 6, 55), fleet: "real" }]);
  });

  it("asks at once on a jump 300 ms or more after the last; in a drag it keeps what it holds, shows no arc ahead of the clock and says loading until 300 ms pass with no jump", async () => {
    const { l, host } = loader(DETECTIONS_CLOCK_MS);
    l.place(host.now, 1, true);
    expect(calls.map((c) => c.params)).toEqual([WINDOW]);
    await answer(calls[0], DETECTIONS.tagged);

    vi.advanceTimersByTime(200);
    host.now = at(19, 40);
    l.place(host.now, 1, true);
    expect(calls).toHaveLength(1);
    expect(l.state(host.now)).toEqual({ status: "loading", local: true });
    expect(l.arcsAt(host.now, true, [], DETECTION_NODES)).toEqual([]);
    // Into the window held: nothing new.
    vi.advanceTimersByTime(200);
    l.place(at(20, 0), 1, true);
    vi.advanceTimersByTime(200);
    l.place(host.now, 1, true);
    vi.advanceTimersByTime(299);
    expect(calls).toHaveLength(1);
    expect(l.state(host.now).status).toBe("loading");

    vi.advanceTimersByTime(1);
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(19, 39, 55), to: at(19, 49, 55), fleet: "real" }]);
    host.now = at(21, 0);
    l.place(host.now, 1, true);
    expect(calls.map((c) => c.params).slice(2)).toEqual([{ from: at(20, 59, 55), to: at(21, 9, 55), fleet: "real" }]);
  });

  it("weighs an answer against the clock as last placed, not against where a drag has gone before its seek lands", async () => {
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    // The pointer is at 20:20 when the window lands; the session lands that seek 200 ms on.
    host.now = at(20, 20);
    await answer(calls[0]);
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(200);
    l.place(host.now, 1, true);
    expect(calls).toHaveLength(1);
  });

  it("keeps what a drag passes, and the window before them it asks for though it lands with the drag gone past it, so a drag back and forth asks for it once", async () => {
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    await answer(calls[0]);
    vi.advanceTimersByTime(1_000);
    host.now = at(19, 58);
    l.place(host.now, 1, true);
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(19, 49, 55), to: at(19, 59, 55), fleet: "real" }]);
    vi.advanceTimersByTime(200);
    host.now = at(20, 5);
    l.place(host.now, 1, true);
    await answer(calls[1]);
    for (const clock of [at(19, 52), at(20, 8), at(19, 58), at(20, 5), at(19, 52), at(20, 8)]) {
      vi.advanceTimersByTime(200);
      host.now = clock;
      l.place(clock, 1, true);
    }
    vi.advanceTimersByTime(1_000);
    expect(calls).toHaveLength(2);
    expect(l.state(at(19, 52))).toEqual({ status: "ready", local: true });
  });

  it("lets go of a start over it holds back on the layer turned off and on leaving, leaving no timer behind, and asks at once once turned on again", async () => {
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    await answer(calls[0]);
    const timers = vi.getTimerCount();
    vi.advanceTimersByTime(200);
    host.now = at(21, 0);
    l.place(host.now, 1, true);
    expect(vi.getTimerCount()).toBe(timers + 1);
    l.disable();
    expect(vi.getTimerCount()).toBe(timers);

    l.enable();
    host.now = at(20, 30);
    l.place(host.now, 1, true);
    expect(calls).toHaveLength(2);
    await answer(calls[1]);
    vi.advanceTimersByTime(200);
    host.now = at(21, 0);
    l.place(host.now, 1, true);
    l.dispose();
    expect(vi.getTimerCount()).toBe(timers);
    vi.advanceTimersByTime(60_000);
    expect(calls).toHaveLength(2);
  });

  it("lets go of a window turned away with a 429 while a drag holds back a start over, and of one waiting out a 429 on a drag away from it", async () => {
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    vi.advanceTimersByTime(200);
    host.now = at(21, 0);
    l.place(host.now, 1, true);
    await refuse(calls[0]);
    expect(l.state(host.now)).toEqual({ status: "loading", local: true });
    expect(vi.getTimerCount()).toBe(1);
    vi.advanceTimersByTime(300);
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(20, 59, 55), to: at(21, 9, 55), fleet: "real" }]);

    await refuse(calls[1]);
    expect(l.state(host.now)).toEqual({ status: "waiting", local: true });
    // A jump the waiting window covers waits with it; the drag's next, away from it, lets go of it.
    l.place(at(21, 1), 1, true);
    vi.advanceTimersByTime(200);
    host.now = at(22, 0);
    l.place(host.now, 1, true);
    expect(l.state(host.now)).toEqual({ status: "loading", local: true });
    expect(vi.getTimerCount()).toBe(1);
    vi.advanceTimersByTime(300);
    expect(calls.map((c) => c.params).slice(2)).toEqual([{ from: at(21, 59, 55), to: at(22, 9, 55), fleet: "real" }]);
  });
});
