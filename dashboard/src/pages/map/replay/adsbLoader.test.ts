import { HttpError } from "@retina/shared";
import { beforeEach, describe, expect, it, vi } from "vitest";

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
    expect(l.state(host.now)).toEqual({ status: "error" });

    // Playback asks for nothing, nor does a seek inside the window held.
    l.place(at(20, 29, 0, 250), 1, false);
    l.place(at(20, 29, 30), 1, false);
    l.place(at(20, 0), 1, true);
    expect(calls).toHaveLength(2);

    l.retry(at(20, 29), 1);
    expect(calls[2].params).toEqual(calls[1].params);
    expect(l.state(at(20, 29)).status).toBe("ready");
    // An answer that cannot be read is a failure too.
    await respond(calls[2], { ...ADSB.traffic, rows: "unreadable" } as unknown as AdsbResponse);
    expect(l.state(at(20, 29)).status).toBe("error");

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
