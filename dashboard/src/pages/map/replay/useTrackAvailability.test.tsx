import { act, cleanup, renderHook } from "@testing-library/react";
import { HttpError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../../api/client";
import type { AvailabilityHour, AvailabilityResponse } from "./types";
import { AVAILABILITY_MAX_DAYS, useTrackAvailability } from "./useTrackAvailability";

vi.mock("../../../api/client", () => ({ api: { archiveTracks: vi.fn(), trackAvailability: vi.fn() } }));

const DAY = 86_400_000;
const at = (iso: string) => Date.parse(iso);
/** The wall clock: today is 2026-10-19. */
const WALL = at("2026-10-19T12:00:00Z");
const CLOCK = at("2026-10-18T20:00:00Z");

const dayMs = (d: string) => at(`${d}T00:00:00Z`);
const dayString = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const shift = (d: string, days: number) => dayString(dayMs(d) + days * DAY);
const hour = (iso: string, count = 10, unclassified = 0): AvailabilityHour => ({
  start_ms: at(iso),
  count,
  unclassified,
});
/** One hour on each day of [from, to]: noon, with `count`. */
function hoursOver(from: string, to: string, count = 10): AvailabilityHour[] {
  const out: AvailabilityHour[] = [];
  for (let ms = dayMs(from); ms <= dayMs(to); ms += DAY) out.push({ start_ms: ms + 12 * 3_600_000, count, unclassified: 0 });
  return out;
}

interface Call {
  params: { from: string; to: string; fleet?: string };
  signal: AbortSignal;
  resolve(resp: AvailabilityResponse): void;
  reject(err: unknown): void;
}
let calls: Call[] = [];
const trackAvailability = vi.mocked(api.trackAvailability);

const body = (call: Call, hours: AvailabilityHour[]): AvailabilityResponse => ({
  from: call.params.from,
  to: call.params.to,
  fleet: "real",
  hours,
  total: hours.reduce((n, h) => n + h.count, 0),
});

async function respond(call: Call, hours: AvailabilityHour[] = hoursOver(call.params.from, call.params.to)) {
  await act(async () => {
    call.resolve(body(call, hours));
  });
}

async function fail(call: Call, err: unknown) {
  await act(async () => {
    call.reject(err);
  });
}

interface Props {
  active: boolean;
  clockMs: number;
}

function setup(over: Partial<Props> = {}) {
  const initialProps: Props = { active: true, clockMs: CLOCK, ...over };
  return renderHook((p: Props) => useTrackAvailability(p), { initialProps });
}

const requested = (call: Call) => [call.params.from, call.params.to];

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(WALL);
  calls = [];
  trackAvailability.mockReset();
  trackAvailability.mockImplementation(
    (params, opts) =>
      new Promise<AvailabilityResponse>((resolve, reject) => {
        calls.push({ params, signal: opts!.signal!, resolve, reject });
      }),
  );
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("the first answer (V2)", () => {
  it("asks once for three days either side of the clock, clipped to today, and holds the hours sorted", async () => {
    const { result } = setup();
    expect(calls).toHaveLength(1);
    expect(calls[0].params).toEqual({ from: "2026-10-15", to: "2026-10-19", fleet: "real" });
    expect(result.current).toMatchObject({ status: "loading", range: null, hours: [], error: null });
    const later = hour("2026-10-18T20:00:00Z", 30, 2);
    const earlier = hour("2026-10-16T03:00:00Z", 12);
    await respond(calls[0], [later, earlier]);
    expect(result.current).toMatchObject({
      status: "ready",
      error: null,
      range: { from: "2026-10-15", to: "2026-10-19" },
    });
    expect(result.current.hours).toEqual([earlier, later]);
    expect(calls).toHaveLength(1);
  });

  it("keeps an hour's cold flag, and an hour without one is not cold", async () => {
    const { result } = setup();
    const cold = { ...hour("2026-10-18T20:00:00Z"), cold: true };
    const warm = hour("2026-10-18T10:00:00Z");
    await respond(calls[0], [warm, cold]);
    expect(result.current.hours.map((h) => h.cold === true)).toEqual([false, true]);
    expect(result.current.hours[1]).toEqual(cold);
  });

  it("takes three whole days either side when today is far enough off", () => {
    setup({ clockMs: at("2026-09-10T23:59:59Z") });
    expect(requested(calls[0])).toEqual(["2026-09-07", "2026-09-13"]);
  });

  it("asks for nothing past today, even for a clock on today or past it", () => {
    setup({ clockMs: WALL });
    expect(requested(calls[0])).toEqual(["2026-10-16", "2026-10-19"]);
    cleanup();
    calls = [];
    setup({ clockMs: at("2026-10-25T00:00:00Z") });
    expect(requested(calls[0])).toEqual(["2026-10-19", "2026-10-19"]);
  });

  it("asks nothing more while the clock stays inside the held days", async () => {
    const { rerender } = setup();
    await respond(calls[0]);
    for (const iso of ["2026-10-18T23:00:00Z", "2026-10-17T01:00:00Z", "2026-10-15T00:00:00Z", "2026-10-19T11:00:00Z"]) {
      rerender({ active: true, clockMs: at(iso) });
    }
    expect(calls).toHaveLength(1);
  });
});

describe("inactive (V1)", () => {
  it("asks for nothing and holds nothing", () => {
    const { result } = setup({ active: false });
    expect(trackAvailability).not.toHaveBeenCalled();
    expect(result.current).toMatchObject({ hours: [], range: null, status: "idle", error: null });
    act(() => result.current.onViewCenter(at("2026-07-01T00:00:00Z")));
    expect(trackAvailability).not.toHaveBeenCalled();
  });

  it("drops what it held and the answer out when it stops being active", async () => {
    const { result, rerender } = setup();
    await respond(calls[0]);
    rerender({ active: true, clockMs: at("2026-09-01T12:00:00Z") });
    const out = calls[1];
    rerender({ active: false, clockMs: at("2026-09-01T12:00:00Z") });
    expect(out.signal.aborted).toBe(true);
    expect(result.current).toMatchObject({ hours: [], range: null, status: "idle", error: null });
    await respond(out);
    expect(result.current).toMatchObject({ hours: [], range: null, status: "idle" });
    expect(calls).toHaveLength(2);
  });
});

describe("following the clock (V3, V5)", () => {
  it("joins the missing days of a nearby day to the held ones", async () => {
    const { result, rerender } = setup();
    await respond(calls[0]);
    const held = result.current.hours;
    rerender({ active: true, clockMs: at("2026-10-13T08:00:00Z") });
    expect(requested(calls[1])).toEqual(["2026-10-10", "2026-10-14"]);
    expect(result.current).toMatchObject({ status: "loading", range: { from: "2026-10-15", to: "2026-10-19" } });
    await respond(calls[1]);
    expect(result.current).toMatchObject({ status: "ready", range: { from: "2026-10-10", to: "2026-10-19" } });
    expect(result.current.hours).toEqual([...hoursOver("2026-10-10", "2026-10-14"), ...held]);
  });

  it("joins a day right after the held ones too", async () => {
    vi.setSystemTime(at("2026-10-30T12:00:00Z"));
    const { result, rerender } = setup();
    expect(requested(calls[0])).toEqual(["2026-10-15", "2026-10-21"]);
    await respond(calls[0]);
    rerender({ active: true, clockMs: at("2026-10-23T08:00:00Z") });
    expect(requested(calls[1])).toEqual(["2026-10-22", "2026-10-26"]);
    await respond(calls[1]);
    expect(result.current.range).toEqual({ from: "2026-10-15", to: "2026-10-26" });
  });

  it("replaces the held days with a week around a day that is not next to them", async () => {
    const { result, rerender } = setup();
    await respond(calls[0]);
    rerender({ active: true, clockMs: at("2026-09-01T12:00:00Z") });
    expect(requested(calls[1])).toEqual(["2026-08-29", "2026-09-04"]);
    const fresh = hoursOver("2026-08-29", "2026-09-04", 3);
    await respond(calls[1], fresh);
    expect(result.current).toMatchObject({ status: "ready", range: { from: "2026-08-29", to: "2026-09-04" } });
    expect(result.current.hours).toEqual(fresh);
  });

  it("keeps one hour per start, the newer answer's, in order", async () => {
    const { result } = setup({ clockMs: WALL });
    expect(requested(calls[0])).toEqual(["2026-10-16", "2026-10-19"]);
    await respond(calls[0], hoursOver("2026-10-16", "2026-10-19", 10));
    // Four days on, a view near the held days' left edge wants them on both sides.
    vi.setSystemTime(at("2026-10-23T12:00:00Z"));
    act(() => result.current.onViewCenter(at("2026-10-17T06:00:00Z")));
    expect(requested(calls[1])).toEqual(["2026-10-14", "2026-10-20"]);
    await respond(calls[1], [
      hour("2026-10-19T12:00:00Z", 99),
      hour("2026-10-14T05:00:00Z", 4),
      hour("2026-10-20T12:00:00Z", 7),
    ]);
    expect(result.current.range).toEqual({ from: "2026-10-14", to: "2026-10-20" });
    expect(result.current.hours).toEqual([
      hour("2026-10-14T05:00:00Z", 4),
      ...hoursOver("2026-10-16", "2026-10-18", 10),
      hour("2026-10-19T12:00:00Z", 99),
      hour("2026-10-20T12:00:00Z", 7),
    ]);
  });
});

describe("following the strip's view (V4, V5)", () => {
  it("extends the held days when the view centres within a day of an edge", async () => {
    const { result } = setup();
    await respond(calls[0]);
    act(() => result.current.onViewCenter(at("2026-10-16T06:00:00Z")));
    expect(requested(calls[1])).toEqual(["2026-10-13", "2026-10-14"]);
    await respond(calls[1]);
    expect(result.current.range).toEqual({ from: "2026-10-13", to: "2026-10-19" });
  });

  it("leaves the held days alone for a view centred clear of both edges", async () => {
    const { result } = setup();
    await respond(calls[0]);
    act(() => result.current.onViewCenter(at("2026-10-17T12:00:00Z")));
    expect(calls).toHaveLength(1);
  });

  it("asks nothing for a view at today's edge, which has nothing further", async () => {
    const { result } = setup();
    await respond(calls[0]);
    act(() => result.current.onViewCenter(at("2026-10-19T06:00:00Z")));
    act(() => result.current.onViewCenter(at("2026-10-18T06:00:00Z")));
    expect(calls).toHaveLength(1);
  });

  it("moves to a week around a view centred far away", async () => {
    const { result } = setup();
    await respond(calls[0]);
    act(() => result.current.onViewCenter(at("2026-07-01T12:00:00Z")));
    expect(requested(calls[1])).toEqual(["2026-06-28", "2026-07-04"]);
    await respond(calls[1]);
    expect(result.current.range).toEqual({ from: "2026-06-28", to: "2026-07-04" });
  });

  it(`joins up to ${AVAILABILITY_MAX_DAYS} days, and past that starts again`, async () => {
    const { result } = setup();
    await respond(calls[0]);
    const span = () => (dayMs(result.current.range!.to) - dayMs(result.current.range!.from)) / DAY + 1;
    const pan = async (centreDay: string) => {
      const n = calls.length;
      act(() => result.current.onViewCenter(dayMs(centreDay) + 12 * 3_600_000));
      expect(calls).toHaveLength(n + 1);
      await respond(calls[n]);
      return calls[n];
    };
    expect(span()).toBe(5);
    // Just outside the left edge: four more days each time.
    for (let i = 0; i < 20; i++) {
      const from = result.current.range!.from;
      const call = await pan(shift(from, -1));
      expect(requested(call)).toEqual([shift(from, -4), shift(from, -1)]);
    }
    expect(span()).toBe(85);
    // On the left edge: three more.
    await pan(result.current.range!.from);
    expect(span()).toBe(88);
    // Four more make exactly the cap, which still joins.
    const to = result.current.range!.to;
    await pan(shift(result.current.range!.from, -1));
    expect(span()).toBe(AVAILABILITY_MAX_DAYS);
    expect(result.current.range!.to).toBe(to);
    // Four more would pass it: the week around the view replaces the lot.
    const centre = shift(result.current.range!.from, -1);
    const fresh = hoursOver(shift(centre, -3), shift(centre, 3), 1);
    const n = calls.length;
    act(() => result.current.onViewCenter(dayMs(centre) + 12 * 3_600_000));
    expect(requested(calls[n])).toEqual([shift(centre, -3), shift(centre, 3)]);
    await respond(calls[n], fresh);
    expect(result.current.range).toEqual({ from: shift(centre, -3), to: shift(centre, 3) });
    expect(result.current.hours).toEqual(fresh);
  });
});

describe("a view zoomed out to a month (E2)", () => {
  const MONTH = 2;

  it("asks for sixteen days either side of its centre, clipped to today, joined to the held ones", async () => {
    const { result } = setup();
    await respond(calls[0]);
    const held = result.current.hours;
    act(() => result.current.onViewCenter(at("2026-10-10T12:00:00Z"), MONTH));
    expect(calls).toHaveLength(2);
    expect(requested(calls[1])).toEqual(["2026-09-24", "2026-10-14"]);
    await respond(calls[1]);
    expect(result.current).toMatchObject({ status: "ready", range: { from: "2026-09-24", to: "2026-10-19" } });
    expect(result.current.hours).toEqual([...hoursOver("2026-09-24", "2026-10-14"), ...held]);
  });

  it("asks for the days it shows that are not held, though its centre is clear of the edges", async () => {
    const { result } = setup();
    await respond(calls[0]);
    act(() => result.current.onViewCenter(at("2026-10-10T12:00:00Z"), MONTH));
    await respond(calls[1]);
    // Held: 09-24..10-19. Centred on 10-05 the month starts at 09-19.
    act(() => result.current.onViewCenter(at("2026-10-05T03:00:00Z"), MONTH));
    expect(calls).toHaveLength(3);
    expect(requested(calls[2])).toEqual(["2026-09-19", "2026-09-23"]);
    await respond(calls[2]);
    expect(result.current.range).toEqual({ from: "2026-09-19", to: "2026-10-19" });
    // Centred anywhere from 10-05 on, every day it shows is held now.
    for (const iso of ["2026-10-05T03:00:00Z", "2026-10-08T12:00:00Z", "2026-10-12T00:00:00Z", "2026-10-19T06:00:00Z"]) {
      act(() => result.current.onViewCenter(at(iso), MONTH));
    }
    expect(calls).toHaveLength(3);
  });

  it("needs nothing new when zoomed back in to a day, while its answer is out or after", async () => {
    const { result } = setup();
    await respond(calls[0]);
    const centre = at("2026-10-10T12:00:00Z");
    act(() => result.current.onViewCenter(centre, MONTH));
    const month = calls[1];
    act(() => result.current.onViewCenter(centre, 1));
    act(() => result.current.onViewCenter(centre, 0));
    expect(calls).toHaveLength(2);
    expect(month.signal.aborted).toBe(false);
    await respond(month);
    expect(result.current.range).toEqual({ from: "2026-09-24", to: "2026-10-19" });
    act(() => result.current.onViewCenter(centre, 1));
    act(() => result.current.onViewCenter(centre));
    expect(calls).toHaveLength(2);
  });

  it(`replaces the held days with the month it shows when joining them would pass ${AVAILABILITY_MAX_DAYS}`, async () => {
    const { result } = setup();
    await respond(calls[0]);
    act(() => result.current.onViewCenter(at("2026-10-03T12:00:00Z"), MONTH));
    expect(requested(calls[1])).toEqual(["2026-09-17", "2026-10-14"]);
    await respond(calls[1]);
    act(() => result.current.onViewCenter(at("2026-09-01T12:00:00Z"), MONTH));
    expect(requested(calls[2])).toEqual(["2026-08-16", "2026-09-16"]);
    await respond(calls[2]);
    expect(result.current.range).toEqual({ from: "2026-08-16", to: "2026-10-19" });
    // 07-16..10-19 would be 96 days.
    act(() => result.current.onViewCenter(at("2026-08-01T12:00:00Z"), MONTH));
    expect(requested(calls[3])).toEqual(["2026-07-16", "2026-08-17"]);
    const fresh = hoursOver("2026-07-16", "2026-08-17", 2);
    await respond(calls[3], fresh);
    expect(result.current.range).toEqual({ from: "2026-07-16", to: "2026-08-17" });
    expect(result.current.hours).toEqual(fresh);
  });
});

describe("today's hours asked for again (E3)", () => {
  const today = (time: string) => at(`2026-10-19T${time}Z`);
  /** Held after the first answer, fetched at WALL (12:00Z): today's 05:00 and 09:00 hours. */
  const FIRST = [...hoursOver("2026-10-16", "2026-10-18"), hour("2026-10-19T05:00:00Z", 3), hour("2026-10-19T09:00:00Z")];

  async function fetchedAtNoon() {
    const hook = setup({ clockMs: today("09:00:00") });
    expect(requested(calls[0])).toEqual(["2026-10-16", "2026-10-19"]);
    await respond(calls[0], FIRST);
    return hook;
  }

  it("once the clock plays into the hour they were fetched in, and then not again within a minute", async () => {
    const { result, rerender } = await fetchedAtNoon();
    vi.setSystemTime(at("2026-10-19T12:59:00Z"));
    rerender({ active: true, clockMs: today("11:59:59.999") });
    expect(calls).toHaveLength(1);
    rerender({ active: true, clockMs: today("12:00:00") });
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual({ from: "2026-10-19", to: "2026-10-19", fleet: "real" });
    // In the background: the strip is not told it is loading.
    expect(result.current.status).toBe("ready");
    // The answer stands for the whole of today: the 05:00 hour it leaves out goes.
    await respond(calls[1], [hour("2026-10-19T09:00:00Z", 12), hour("2026-10-19T12:00:00Z", 7)]);
    expect(result.current).toMatchObject({ status: "ready", error: null, range: { from: "2026-10-16", to: "2026-10-19" } });
    expect(result.current.hours).toEqual([
      ...hoursOver("2026-10-16", "2026-10-18"),
      hour("2026-10-19T09:00:00Z", 12),
      hour("2026-10-19T12:00:00Z", 7),
    ]);
    // Answered in the 12:00 hour, which the clock is in: a minute after asking, not before.
    vi.setSystemTime(at("2026-10-19T12:59:59.999Z"));
    rerender({ active: true, clockMs: today("12:00:01") });
    rerender({ active: true, clockMs: today("12:30:00") });
    expect(calls).toHaveLength(2);
    vi.setSystemTime(at("2026-10-19T13:00:00Z"));
    rerender({ active: true, clockMs: today("12:30:01") });
    expect(calls).toHaveLength(3);
    expect(requested(calls[2])).toEqual(["2026-10-19", "2026-10-19"]);
  });

  it("not while today is not held", async () => {
    const { rerender } = await fetchedAtNoon();
    // Past midnight the held days end yesterday; the clock is still in them, past the hour.
    vi.setSystemTime(at("2026-10-20T00:30:00Z"));
    rerender({ active: true, clockMs: today("23:00:00") });
    rerender({ active: true, clockMs: today("23:30:00") });
    expect(calls).toHaveLength(1);
    cleanup();
    calls = [];
    // Held days nowhere near today.
    vi.setSystemTime(WALL);
    const far = setup({ clockMs: at("2026-10-01T12:00:00Z") });
    await respond(calls[0]);
    vi.setSystemTime(at("2026-10-19T18:00:00Z"));
    far.rerender({ active: true, clockMs: at("2026-10-01T13:00:00Z") });
    far.rerender({ active: true, clockMs: at("2026-10-03T23:00:00Z") });
    expect(calls).toHaveLength(1);
  });

  it("wait for a request out, and are dropped by a newer need", async () => {
    const { result, rerender } = await fetchedAtNoon();
    const held = result.current.hours;
    vi.setSystemTime(at("2026-10-19T13:30:00Z"));
    rerender({ active: true, clockMs: today("12:10:00") });
    const refresh = calls[1];
    expect(requested(refresh)).toEqual(["2026-10-19", "2026-10-19"]);
    act(() => result.current.onViewCenter(at("2026-10-16T06:00:00Z")));
    expect(refresh.signal.aborted).toBe(true);
    expect(requested(calls[2])).toEqual(["2026-10-13", "2026-10-15"]);
    rerender({ active: true, clockMs: today("12:10:01") });
    expect(calls).toHaveLength(3);
    await respond(refresh, [hour("2026-10-19T12:00:00Z", 99)]);
    expect(result.current.hours).toBe(held);
    await respond(calls[2]);
    expect(result.current.range).toEqual({ from: "2026-10-13", to: "2026-10-19" });
    expect(result.current.hours.some((h) => h.count === 99)).toBe(false);
    // Nothing out, and a minute since today was asked for.
    vi.setSystemTime(at("2026-10-19T13:31:00Z"));
    rerender({ active: true, clockMs: today("12:10:02") });
    expect(calls).toHaveLength(4);
    expect(requested(calls[3])).toEqual(["2026-10-19", "2026-10-19"]);
  });

  it("say when they fail, keep the held hours, and try again a minute on", async () => {
    const { result, rerender } = await fetchedAtNoon();
    const held = result.current.hours;
    vi.setSystemTime(at("2026-10-19T13:00:00Z"));
    rerender({ active: true, clockMs: today("12:00:00") });
    await fail(calls[1], new HttpError(503, "Service Unavailable", null));
    expect(result.current).toMatchObject({ status: "error", error: "HTTP 503", range: { from: "2026-10-16", to: "2026-10-19" } });
    expect(result.current.hours).toBe(held);
    rerender({ active: true, clockMs: today("12:00:01") });
    expect(calls).toHaveLength(2);
    vi.setSystemTime(at("2026-10-19T13:01:00Z"));
    rerender({ active: true, clockMs: today("12:00:02") });
    expect(calls).toHaveLength(3);
    await respond(calls[2], [hour("2026-10-19T12:00:00Z", 7)]);
    expect(result.current).toMatchObject({ status: "ready", error: null });
    expect(result.current.hours.slice(-1)).toEqual([hour("2026-10-19T12:00:00Z", 7)]);
  });
});

describe("a 429 from the archive", () => {
  /** The archive's answer to a client asking too often, naming `retryAfterMs` as its wait (null: none the page can read). */
  const limited = (retryAfterMs: number | null) => new HttpError(429, "Too Many Requests", null, retryAfterMs);
  const advance = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  it("is waited out rather than failed, five seconds when it names no wait, then the same days are asked for again", async () => {
    const { result } = setup();
    await fail(calls[0], limited(null));
    expect(result.current).toMatchObject({ status: "waiting", error: null, range: null, hours: [] });
    await advance(4_999);
    expect(calls).toHaveLength(1);
    await advance(1);
    expect(calls).toHaveLength(2);
    expect(calls[1].params).toEqual(calls[0].params);
    expect(result.current.status).toBe("loading");
    await respond(calls[1]);
    expect(result.current).toMatchObject({ status: "ready", error: null, range: { from: "2026-10-15", to: "2026-10-19" } });
  });

  it("keeps the held hours while it waits, and is let go of for a need elsewhere, which asks at once, leaving no timer behind", async () => {
    const { result, rerender } = setup();
    await respond(calls[0]);
    const held = result.current.hours;
    rerender({ active: true, clockMs: at("2026-09-01T12:00:00Z") });
    const timers = vi.getTimerCount();
    await fail(calls[1], limited(20_000));
    expect(result.current).toMatchObject({ status: "waiting", range: { from: "2026-10-15", to: "2026-10-19" } });
    expect(result.current.hours).toBe(held);
    expect(vi.getTimerCount()).toBe(timers + 1);
    rerender({ active: true, clockMs: at("2026-07-01T12:00:00Z") });
    expect(vi.getTimerCount()).toBe(timers);
    expect(calls).toHaveLength(3);
    expect(requested(calls[2])).toEqual(["2026-06-28", "2026-07-04"]);
    await advance(60_000);
    expect(calls).toHaveLength(3);
  });

  it("is let go of on leaving archive mode, leaving no timer behind and asking for nothing later", async () => {
    const { rerender } = setup();
    const timers = vi.getTimerCount();
    await fail(calls[0], limited(20_000));
    expect(vi.getTimerCount()).toBe(timers + 1);
    rerender({ active: false, clockMs: CLOCK });
    expect(vi.getTimerCount()).toBe(timers);
    await advance(60_000);
    expect(calls).toHaveLength(1);
  });

  it("on today's hours asked for again waits as quietly as that request loads", async () => {
    const { result, rerender } = setup({ clockMs: at("2026-10-19T09:00:00Z") });
    await respond(calls[0]);
    vi.setSystemTime(at("2026-10-19T13:00:00Z"));
    rerender({ active: true, clockMs: at("2026-10-19T12:00:00Z") });
    expect(requested(calls[1])).toEqual(["2026-10-19", "2026-10-19"]);
    await fail(calls[1], limited(2_000));
    expect(result.current).toMatchObject({ status: "ready", error: null });
    await advance(2_000);
    expect(calls).toHaveLength(3);
    expect(requested(calls[2])).toEqual(["2026-10-19", "2026-10-19"]);
    expect(result.current.status).toBe("ready");
  });
});

describe("one request at a time", () => {
  it("drops the answer out for an older need, and takes the newer one's", async () => {
    const { result } = setup();
    const first = calls[0];
    act(() => result.current.onViewCenter(at("2026-07-01T12:00:00Z")));
    expect(first.signal.aborted).toBe(true);
    expect(requested(calls[1])).toEqual(["2026-06-28", "2026-07-04"]);
    await respond(first);
    expect(result.current).toMatchObject({ status: "loading", range: null });
    await respond(calls[1]);
    expect(result.current).toMatchObject({ status: "ready", range: { from: "2026-06-28", to: "2026-07-04" } });
  });

  it("keeps a request out that will answer the newer need", async () => {
    const { result } = setup();
    act(() => result.current.onViewCenter(at("2026-10-17T12:00:00Z")));
    expect(calls).toHaveLength(1);
    expect(calls[0].signal.aborted).toBe(false);
  });

  it("drops a request out that would replace held days the newer need is inside", async () => {
    const { result } = setup();
    await respond(calls[0]);
    act(() => result.current.onViewCenter(at("2026-07-01T12:00:00Z")));
    act(() => result.current.onViewCenter(at("2026-10-17T12:00:00Z")));
    expect(calls[1].signal.aborted).toBe(true);
    expect(result.current).toMatchObject({ status: "ready", range: { from: "2026-10-15", to: "2026-10-19" } });
    await respond(calls[1]);
    expect(result.current.range).toEqual({ from: "2026-10-15", to: "2026-10-19" });
  });
});

describe("errors", () => {
  it.each([
    [new HttpError(503, "Service Unavailable", null), "HTTP 503"],
    [new HttpError(422, "Unprocessable Entity", { detail: "the range is capped at 92 days" }), "the range is capped at 92 days"],
    [new DOMException("No response within 15000 ms", "TimeoutError"), "timed out"],
    [new TypeError("Failed to fetch"), "network error"],
    [new HttpError(400, "Bad Request", { detail: "to must not be before from" }), "to must not be before from"],
    [new HttpError(404, "Not Found", { detail: "Not Found" }), "HTTP 404"],
    [new HttpError(502, "Bad Gateway", null), "HTTP 502"],
    [new Error("boom"), "unexpected error"],
  ])("say %s as %j", async (err, message) => {
    const { result } = setup();
    await fail(calls[0], err);
    expect(result.current).toMatchObject({ status: "error", error: message, range: null, hours: [] });
  });

  it("keep the held hours, and the next need asks again", async () => {
    const { result, rerender } = setup();
    await respond(calls[0]);
    const held = result.current.hours;
    rerender({ active: true, clockMs: at("2026-10-13T08:00:00Z") });
    await fail(calls[1], new HttpError(503, "Service Unavailable", null));
    expect(result.current).toMatchObject({ status: "error", error: "HTTP 503", range: { from: "2026-10-15", to: "2026-10-19" } });
    expect(result.current.hours).toBe(held);
    act(() => result.current.onViewCenter(at("2026-10-13T08:00:00Z")));
    expect(requested(calls[2])).toEqual(["2026-10-10", "2026-10-14"]);
    await respond(calls[2]);
    expect(result.current).toMatchObject({ status: "ready", error: null, range: { from: "2026-10-10", to: "2026-10-19" } });
  });

  it("ignore an abort", async () => {
    const { result } = setup();
    await fail(calls[0], new DOMException("The operation was aborted.", "AbortError"));
    expect(result.current).toMatchObject({ status: "idle", error: null });
  });
});
