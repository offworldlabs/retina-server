import { HttpError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../../api/client";
import { ADSB, ADSB_CLOCK_MS, GAP_HEX, KNOWN_LANE_HEX, NO_CALLSIGN_HEX, NO_SOLVE_HEX } from "./__fixtures__/adsb";
import { AdsbLoader } from "./adsbLoader";
import type { AdsbWindow } from "./adsbTraffic";
import type { AdsbResponse } from "./types";

vi.mock("../../../api/client", () => ({ api: { archiveAdsb: vi.fn() } }));

const at = (h: number, m: number, s = 0, ms = 0) => Date.UTC(2026, 9, 18, h, m, s, ms);

interface Call {
  params: { from: number; to: number; fleet?: string; bbox?: unknown };
  signal: AbortSignal;
  resolve(resp: AdsbResponse): void;
  reject(err: unknown): void;
}
let calls: Call[] = [];
const archiveAdsb = vi.mocked(api.archiveAdsb);

/** A loader on a session whose clock, rate and archive edge the test sets, turned on. */
function loader(clock = ADSB_CLOCK_MS, rate = 1) {
  const host = { now: clock, rate, edge: at(23, 0), landed: [] as (AdsbWindow | null)[] };
  const l = new AdsbLoader({
    clock: () => host.now,
    rate: () => host.rate,
    edge: () => host.edge,
    changed: (w) => host.landed.push(w),
  });
  l.enable();
  return { l, host };
}

const settle = () => new Promise((r) => setTimeout(r, 0));
/** Wall time moves on a second and no timer fires: the next jump comes on its own, not in a drag. */
const apart = () => vi.setSystemTime(Date.now() + 1_000);

async function respond(call: Call, resp: AdsbResponse) {
  call.resolve(resp);
  await settle();
}

const hexesAt = (l: AdsbLoader, ms: number) => Object.keys(l.trafficAt(ms).ground_truth).sort();

beforeEach(() => {
  calls = [];
  archiveAdsb.mockReset();
  archiveAdsb.mockImplementation(
    (params, opts) =>
      new Promise<AdsbResponse>((resolve, reject) => {
        const signal = opts!.signal!;
        signal.addEventListener("abort", () => reject(signal.reason));
        calls.push({ params, signal, resolve, reject });
      }),
  );
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the ADS-B loader", () => {
  it("asks for nothing while off, then for the real fleet's hour centred on the clock, with no bbox", async () => {
    const off = new AdsbLoader({ clock: () => ADSB_CLOCK_MS, rate: () => 1, edge: () => at(23, 0), changed: () => {} });
    off.place(ADSB_CLOCK_MS, 1, true);
    off.place(ADSB_CLOCK_MS + 250, 1, false);
    expect(calls).toHaveLength(0);
    expect(off.state(ADSB_CLOCK_MS)).toEqual({ status: "off" });
    expect(off.trafficAt(ADSB_CLOCK_MS)).toEqual({ ground_truth: {}, ground_truth_meta: {} });

    const { l, host } = loader();
    l.place(ADSB_CLOCK_MS, 1, true);
    expect(calls.map((c) => c.params)).toEqual([{ from: at(19, 30), to: at(20, 30), fleet: "real" }]);
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "loading" });
    expect(l.trafficAt(ADSB_CLOCK_MS)).toEqual({ ground_truth: {}, ground_truth_meta: {} });

    await respond(calls[0], ADSB.traffic);
    expect(host.landed).toHaveLength(1);
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "ready" });
    expect(hexesAt(l, ADSB_CLOCK_MS)).toEqual([NO_SOLVE_HEX, KNOWN_LANE_HEX, NO_CALLSIGN_HEX].sort());
    expect(hexesAt(l, at(19, 59, 10))).toContain(GAP_HEX);
  });

  it.each([
    [1, at(20, 29)],
    [5, at(20, 28, 45)],
    [20, at(20, 25)],
  ])("at %ix asks for the next hour once the clock is the margin before the held window ends", async (rate, due) => {
    const { l, host } = loader(ADSB_CLOCK_MS, rate);
    l.place(ADSB_CLOCK_MS, rate, true);
    await respond(calls[0], ADSB.traffic);
    host.now = due - 1;
    l.place(due - 1, rate, false);
    expect(calls).toHaveLength(1);
    host.now = due;
    l.place(due, rate, false);
    expect(calls.map((c) => c.params)).toEqual([
      { from: at(19, 30), to: at(20, 30), fleet: "real" },
      { from: at(20, 30), to: at(21, 30), fleet: "real" },
    ]);
    // One out at a time.
    l.place(due + 250, rate, false);
    expect(calls).toHaveLength(2);
  });

  it("drops the windows and the request out on a seek away from them, and asks for the hour around the new clock", async () => {
    const { l, host } = loader();
    l.place(ADSB_CLOCK_MS, 1, true);
    await respond(calls[0], ADSB.traffic);
    host.now = at(20, 29);
    l.place(host.now, 1, false);
    const out = calls[1];

    // Inside the held window from 20 s after its start, or inside the one out: nothing.
    l.place(at(19, 30, 20), 1, true);
    l.place(at(20, 34), 1, true);
    expect(calls).toHaveLength(2);
    expect(out.signal.aborted).toBe(false);

    // Within 20 s of the held window's start, it cannot say which aircraft were there.
    apart();
    l.place(at(19, 30, 19), 1, true);
    expect(out.signal.aborted).toBe(true);
    expect(calls[2].params).toEqual({ from: at(19, 0, 19), to: at(20, 0, 19), fleet: "real" });
    await settle();
    // The abort is no failure, and the old window serves nothing now.
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "loading" });
    expect(l.trafficAt(ADSB_CLOCK_MS)).toEqual({ ground_truth: {}, ground_truth_meta: {} });
  });

  it("goes on from a cut answer's last snapshot on a window as long as what it served, and back to an hour after a whole one", async () => {
    const { l } = loader();
    l.place(ADSB_CLOCK_MS, 1, true);
    await respond(calls[0], ADSB.truncated);
    // Cut 10 s in: the rest is asked for at once, from the cut snapshot, on the one-minute floor.
    expect(calls[1].params).toEqual({ from: at(19, 30, 10), to: at(19, 31, 10), fleet: "real" });
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "loading" });

    await respond(calls[1], ADSB.afterTruncation);
    expect(calls[2].params).toEqual({ from: at(19, 31, 10), to: at(20, 31, 10), fleet: "real" });
    expect(calls).toHaveLength(3);
  });

  it("stops asking after a failure until a seek away from the windows held, a retry, or the layer turned off and on", async () => {
    const { l, host } = loader();
    l.place(ADSB_CLOCK_MS, 1, true);
    await respond(calls[0], ADSB.traffic);
    host.now = at(20, 29);
    l.place(host.now, 1, false);
    calls[1].reject(new HttpError(503, "Service Unavailable", null));
    await settle();
    expect(host.landed[host.landed.length - 1]).toBeNull();
    // The window held still shows the clock after the next one fails.
    expect(l.state(host.now)).toEqual({ status: "ready" });

    // Playback asks for nothing, nor does a seek inside the window held. Once
    // play leaves that window, the failure is why nothing shows the clock.
    l.place(at(20, 29, 0, 250), 1, false);
    l.place(at(20, 29, 30), 1, false);
    l.place(at(20, 0), 1, true);
    l.place(at(20, 30, 30), 1, false);
    expect(calls).toHaveLength(2);
    expect(l.state(at(20, 30, 30))).toEqual({ status: "error" });

    l.retry(at(20, 29), 1);
    expect(calls[2].params).toEqual(calls[1].params);
    expect(l.state(at(20, 29)).status).toBe("ready");
    expect(l.state(at(20, 30, 30)).status).toBe("loading");
    // An answer that cannot be read is a failure too.
    await respond(calls[2], { ...ADSB.traffic, rows: "unreadable" } as unknown as AdsbResponse);
    expect(l.state(at(20, 29)).status).toBe("ready");
    expect(l.state(at(20, 30, 30)).status).toBe("error");

    apart();
    l.place(at(22, 0), 1, true);
    expect(calls[3].params).toEqual({ from: at(21, 30), to: at(22, 30), fleet: "real" });
    expect(l.state(at(22, 0)).status).toBe("loading");

    calls[3].reject(new HttpError(500, "Internal Server Error", null));
    await settle();
    expect(l.state(at(22, 0)).status).toBe("error");
    l.disable();
    expect(l.state(at(22, 0))).toEqual({ status: "off" });
    l.enable();
    l.place(at(22, 0), 1, true);
    expect(calls).toHaveLength(5);
    expect(l.state(at(22, 0)).status).toBe("loading");
  });

  it("near the archive's edge, asks for more only once the archive holds a minute past what is held", async () => {
    const { l, host } = loader();
    host.edge = at(20, 0, 30);
    l.place(ADSB_CLOCK_MS, 1, true);
    await respond(calls[0], ADSB.traffic);
    // It serves up to the edge as it stood when it landed.
    expect(l.serves(at(20, 0, 30))).toBe(true);
    expect(l.serves(at(20, 0, 30, 1))).toBe(false);

    for (let ms = ADSB_CLOCK_MS; ms <= at(20, 0, 30); ms += 250) {
      host.now = ms;
      l.place(ms, 1, false);
    }
    expect(calls).toHaveLength(1);

    host.edge = at(20, 1, 30);
    l.place(host.now, 1, false);
    expect(calls).toHaveLength(1);
    host.edge = at(20, 1, 30, 1);
    l.place(host.now, 1, false);
    expect(calls.map((c) => c.params.from)).toEqual([at(19, 30), at(20, 0, 30, 1)]);
  });
});

describe("the ADS-B loader when the traffic cannot be built", () => {
  it("says error for the clock though a window held shows it", async () => {
    const { l, host } = loader();
    l.place(ADSB_CLOCK_MS, 1, true);
    await respond(calls[0], ADSB.traffic);
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "ready" });
    Object.defineProperty(host.landed[0], "hexes", {
      get() {
        throw new Error("window unreadable");
      },
    });
    expect(l.trafficAt(ADSB_CLOCK_MS)).toEqual({ ground_truth: {}, ground_truth_meta: {} });
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "error" });
  });
});

describe("the ADS-B loader on a 429 from the archive", () => {
  /** The archive's answer to a client asking too often, naming `retryAfterMs` as its wait. */
  const limited = (retryAfterMs: number | null) => new HttpError(429, "Too Many Requests", null, retryAfterMs);
  const HOUR = { from: at(19, 30), to: at(20, 30), fleet: "real" };

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.setSystemTime(at(23, 0));
  });

  async function refuse(call: Call, retryAfterMs: number | null) {
    call.reject(limited(retryAfterMs));
    await vi.advanceTimersByTimeAsync(0);
  }

  it("is waited out rather than failed, then the same hour is asked for again on its own once the wait is over", async () => {
    const { l, host } = loader();
    l.place(ADSB_CLOCK_MS, 1, true);
    await refuse(calls[0], 7_000);
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "waiting" });
    expect(host.landed).toEqual([null]);
    vi.advanceTimersByTime(6_999);
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(calls.map((c) => c.params)).toEqual([HOUR, HOUR]);
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "loading" });
    calls[1].resolve(ADSB.traffic);
    await vi.advanceTimersByTimeAsync(0);
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "ready" });
  });

  it("keeps showing the hour held while the next one waits, at most a minute, and asks for nothing else meanwhile", async () => {
    const { l, host } = loader();
    l.place(ADSB_CLOCK_MS, 1, true);
    calls[0].resolve(ADSB.traffic);
    await vi.advanceTimersByTimeAsync(0);
    host.now = at(20, 29);
    l.place(host.now, 1, false);
    expect(calls).toHaveLength(2);
    await refuse(calls[1], 3_600_000);
    expect(l.state(host.now)).toEqual({ status: "ready" });
    // Played past the hour held, the clock waits with it.
    l.place(at(20, 30, 30), 1, false);
    expect(l.state(at(20, 30, 30))).toEqual({ status: "waiting" });
    vi.advanceTimersByTime(59_999);
    expect(calls).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(calls).toHaveLength(3);
    expect(calls[2].params).toEqual(calls[1].params);
  });

  it("is let go of on a jump away, on the layer turned off and on leaving, leaving no timer behind and asking for nothing later", async () => {
    const { l, host } = loader();
    l.place(ADSB_CLOCK_MS, 1, true);
    const timers = vi.getTimerCount();
    await refuse(calls[0], 5_000);
    expect(vi.getTimerCount()).toBe(timers + 1);
    // A jump inside the hour waited for waits with it; one away asks for its own hour at once.
    l.place(at(20, 20), 1, true);
    expect(calls).toHaveLength(1);
    expect(l.state(at(20, 20))).toEqual({ status: "waiting" });
    apart();
    host.now = at(22, 0);
    l.place(host.now, 1, true);
    expect(calls.map((c) => c.params)).toEqual([HOUR, { from: at(21, 30), to: at(22, 30), fleet: "real" }]);
    expect(vi.getTimerCount()).toBe(timers);
    vi.advanceTimersByTime(60_000);
    expect(calls).toHaveLength(2);

    await refuse(calls[1], 5_000);
    l.disable();
    expect(vi.getTimerCount()).toBe(timers);
    expect(l.state(host.now)).toEqual({ status: "off" });
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

describe("the ADS-B loader while the scrubber is dragged", () => {
  const NO_TRAFFIC = { ground_truth: {}, ground_truth_meta: {} };

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    vi.setSystemTime(at(23, 0));
  });

  /** A whole answer with no rows for the window `call` asked for. */
  const empty = (call: Call): AdsbResponse => ({
    from: call.params.from,
    to: call.params.to,
    fleet: "real",
    count: 0,
    truncated: false,
    rows: [],
  });

  async function answer(call: Call, resp = empty(call)) {
    call.resolve(resp);
    await vi.advanceTimersByTimeAsync(0);
  }

  async function refuse(call: Call) {
    call.reject(new HttpError(429, "Too Many Requests", null, 5_000));
    await vi.advanceTimersByTimeAsync(0);
  }

  it("asks for two hours in a minute of sweeps across two hours, a sweep each 500 ms: one as the drag starts, one where it settles", async () => {
    // The session lands a dragged seek every 200 ms: at 19:00, 19:48, 20:36, 20:36, 19:48, and round again.
    const landed = [at(19, 0), at(19, 48), at(20, 36), at(20, 36), at(19, 48)];
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
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(18, 30), to: at(19, 30), fleet: "real" }]);
    vi.advanceTimersByTime(99);
    expect(calls).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(calls.map((c) => c.params).slice(2)).toEqual([{ from: at(19, 18), to: at(20, 18), fleet: "real" }]);
  });

  it("asks at once on a seek right after the layer is turned on", () => {
    const { l, host } = loader();
    l.place(host.now, 1, true);
    host.now = at(22, 0);
    l.place(host.now, 1, true);
    expect(calls.map((c) => c.params)).toEqual([
      { from: at(19, 30), to: at(20, 30), fleet: "real" },
      { from: at(21, 30), to: at(22, 30), fleet: "real" },
    ]);
  });

  it("weighs an answer against the clock as last placed; in a drag it keeps what it holds, shows no traffic it does not hold and says loading until 300 ms pass with no jump, and asks at once on a jump 300 ms after the last", async () => {
    const { l, host } = loader();
    l.place(host.now, 1, true);
    expect(calls.map((c) => c.params)).toEqual([{ from: at(19, 30), to: at(20, 30), fleet: "real" }]);
    vi.advanceTimersByTime(1_000);
    l.place(at(20, 10), 1, true);
    // The pointer is at 21:00 when the hour lands; the session lands that seek 200 ms on.
    host.now = at(21, 0);
    await answer(calls[0], ADSB.traffic);
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(200);
    l.place(host.now, 1, true);
    expect(calls).toHaveLength(1);
    expect(l.state(host.now)).toEqual({ status: "loading" });
    expect(l.trafficAt(host.now)).toEqual(NO_TRAFFIC);
    // Back into the hour held: it still shows it, and nothing is asked for.
    vi.advanceTimersByTime(200);
    l.place(ADSB_CLOCK_MS, 1, true);
    expect(l.state(ADSB_CLOCK_MS)).toEqual({ status: "ready" });
    expect(hexesAt(l, ADSB_CLOCK_MS)).toEqual([NO_SOLVE_HEX, KNOWN_LANE_HEX, NO_CALLSIGN_HEX].sort());
    vi.advanceTimersByTime(200);
    l.place(host.now, 1, true);
    vi.advanceTimersByTime(299);
    expect(calls).toHaveLength(1);
    expect(l.state(host.now).status).toBe("loading");

    vi.advanceTimersByTime(1);
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(20, 30), to: at(21, 30), fleet: "real" }]);
    host.now = at(22, 0);
    l.place(host.now, 1, true);
    expect(calls.map((c) => c.params).slice(2)).toEqual([{ from: at(21, 30), to: at(22, 30), fleet: "real" }]);
  });

  it("keeps the hours a drag passes, so a drag back and forth across them asks for nothing", async () => {
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    await answer(calls[0]);
    host.now = at(20, 29);
    l.place(host.now, 1, false);
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(20, 30), to: at(21, 30), fleet: "real" }]);
    await answer(calls[1]);
    vi.advanceTimersByTime(1_000);
    for (const clock of [at(20, 0), at(21, 20), at(20, 0), at(21, 20), at(20, 0)]) {
      host.now = clock;
      l.place(clock, 1, true);
      vi.advanceTimersByTime(200);
    }
    vi.advanceTimersByTime(1_000);
    expect(calls).toHaveLength(2);
    expect(l.state(at(20, 0))).toEqual({ status: "ready" });
  });

  it("asks for the next hour as the drag settles when play has come within the margin of the hour held meanwhile", async () => {
    const { l, host } = loader(at(20, 0), 20);
    l.place(host.now, 20, true);
    await answer(calls[0]);
    vi.advanceTimersByTime(1_000);
    host.now = at(20, 20);
    l.place(host.now, 20, true);
    vi.advanceTimersByTime(200);
    host.now = at(20, 26);
    l.place(host.now, 20, true);
    // Playing at 20x, the margin is 5 minutes: the next hour is due.
    host.now = at(20, 26, 5);
    l.place(host.now, 20, false);
    vi.advanceTimersByTime(299);
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(20, 30), to: at(21, 30), fleet: "real" }]);
  });

  it("says loading, not error, while a drag holds back the start over after a failure", async () => {
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    await answer(calls[0]);
    host.now = at(20, 29);
    l.place(host.now, 1, false);
    calls[1].reject(new HttpError(503, "Service Unavailable", null));
    await vi.advanceTimersByTimeAsync(0);
    vi.advanceTimersByTime(1_000);
    l.place(at(20, 10), 1, true);
    vi.advanceTimersByTime(200);
    host.now = at(22, 0);
    l.place(host.now, 1, true);
    expect(l.state(host.now)).toEqual({ status: "loading" });
    vi.advanceTimersByTime(300);
    expect(calls.map((c) => c.params).slice(2)).toEqual([{ from: at(21, 30), to: at(22, 30), fleet: "real" }]);
  });

  it("lets go of a start over it holds back on the layer turned off and on leaving, leaving no timer behind, and asks at once once turned on again", async () => {
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    await answer(calls[0]);
    const timers = vi.getTimerCount();
    vi.advanceTimersByTime(1_000);
    l.place(at(20, 10), 1, true);
    vi.advanceTimersByTime(200);
    host.now = at(22, 0);
    l.place(host.now, 1, true);
    expect(vi.getTimerCount()).toBe(timers + 1);
    l.disable();
    expect(vi.getTimerCount()).toBe(timers);

    l.enable();
    host.now = at(21, 0);
    l.place(host.now, 1, true);
    l.place(at(21, 10), 1, true);
    expect(calls).toHaveLength(2);
    await answer(calls[1]);
    vi.advanceTimersByTime(200);
    host.now = at(22, 30);
    l.place(host.now, 1, true);
    l.dispose();
    expect(vi.getTimerCount()).toBe(timers);
    vi.advanceTimersByTime(60_000);
    expect(calls).toHaveLength(2);
  });

  it("lets go of an hour turned away with a 429 while a drag holds back a start over, and of one waiting out a 429 on a drag away from it", async () => {
    const { l, host } = loader(at(20, 0));
    l.place(host.now, 1, true);
    vi.advanceTimersByTime(1_000);
    l.place(at(20, 10), 1, true);
    vi.advanceTimersByTime(200);
    host.now = at(22, 0);
    l.place(host.now, 1, true);
    await refuse(calls[0]);
    expect(l.state(host.now)).toEqual({ status: "loading" });
    expect(vi.getTimerCount()).toBe(1);
    vi.advanceTimersByTime(300);
    expect(calls.map((c) => c.params).slice(1)).toEqual([{ from: at(21, 30), to: at(22, 30), fleet: "real" }]);

    await refuse(calls[1]);
    expect(l.state(host.now)).toEqual({ status: "waiting" });
    // A jump the waiting hour covers waits with it; the drag's next, away from it, lets go of it.
    l.place(at(22, 1), 1, true);
    vi.advanceTimersByTime(200);
    host.now = at(20, 0);
    l.place(host.now, 1, true);
    expect(l.state(host.now)).toEqual({ status: "loading" });
    expect(vi.getTimerCount()).toBe(1);
    vi.advanceTimersByTime(300);
    expect(calls.map((c) => c.params).slice(2)).toEqual([{ from: at(19, 30), to: at(20, 30), fleet: "real" }]);
  });
});
