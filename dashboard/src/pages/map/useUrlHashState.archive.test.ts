import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ARCHIVE_SPAN_DEFAULT_MIN,
  formatHashT,
  parseHash,
  useArchiveHash,
  useHashWriter,
  writeArchiveHash,
} from "./useUrlHashState";

// The archive keys of the map's hash: `t`, the instant a link replays, and
// `span`, the window's radius in minutes.

const NOW = Date.UTC(2026, 9, 19, 12, 0, 0);
/** 2026-10-18T22:14:00Z, the instant most links below name. */
const T = Date.UTC(2026, 9, 18, 22, 14, 0);
const T_TEXT = "2026-10-18T22:14:00Z";
const DAY_MS = 24 * 60 * 60 * 1000;

const at = (url: string) => window.history.replaceState(null, "", url);

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  at("/");
});

describe("parseHash: t", () => {
  it.each([
    ["Z", "2026-10-18T22:14:00Z", T],
    ["no seconds", "2026-10-18T22:14Z", T],
    ["no zone, read as UTC", "2026-10-18T22:14:00", T],
    ["no seconds and no zone", "2026-10-18T22:14", T],
    ["lowercase t and z", "2026-10-18t22:14:00z", T],
    ["+HH:MM", "2026-10-19T00:14:00+02:00", T],
    ["+HHMM", "2026-10-19T00:14:00+0200", T],
    ["-HH:MM", "2026-10-18T17:14:00-05:00", T],
    ["-HHMM without seconds", "2026-10-18T17:14-0500", T],
    ["a half-hour offset", "2026-10-19T03:44:00+05:30", T],
    ["-00:00", "2026-10-18T22:14:00-00:00", T],
    ["one fractional digit", "2026-10-18T22:14:00.5Z", T + 500],
    ["two fractional digits", "2026-10-18T22:14:00.25Z", T + 250],
    ["three fractional digits", "2026-10-18T22:14:00.123Z", T + 123],
    ["a fraction and an offset", "2026-10-19T00:14:00.123+02:00", T + 123],
    ["percent-encoded", "2026-10-19T00%3A14%3A00%2B02%3A00", T],
    ["a leap day", "2024-02-29T12:00:00Z", Date.UTC(2024, 1, 29, 12)],
    ["the earliest instant", "2020-01-01T00:00:00Z", Date.UTC(2020, 0, 1)],
    ["24 h ahead of the clock, to the millisecond", "2026-10-20T12:00:00Z", NOW + DAY_MS],
  ])("reads %s", (_, value, expected) => {
    expect(parseHash(`#lat=1&t=${value}&z=5`, NOW)).toEqual({ lat: 1, t: expected, z: 5 });
  });

  it.each([
    ["garbage", "yesterday"],
    ["an empty value", ""],
    ["a date alone", "2026-10-18"],
    ["hours alone", "2026-10-18T22Z"],
    ["a space for T", "2026-10-18 22:14:00Z"],
    ["trailing text", "2026-10-18T22:14:00Z!"],
    ["four fractional digits", "2026-10-18T22:14:00.1234Z"],
    ["a dot with no digits", "2026-10-18T22:14:00.Z"],
    ["a fraction of a minute", "2026-10-18T22:14.5Z"],
    ["a one-digit offset", "2026-10-18T22:14:00+2:00"],
    ["an offset in hours alone", "2026-10-18T22:14:00+02"],
    ["a zone name", "2026-10-18T22:14:00UTC"],
    ["epoch milliseconds", String(T)],
    ["a two-digit year", "26-10-18T22:14:00Z"],
    ["30 February", "2026-02-30T00:00Z"],
    ["29 February in a common year", "2025-02-29T00:00Z"],
    ["31 April", "2026-04-31T00:00Z"],
    ["month 13", "2026-13-01T00:00Z"],
    ["month 00", "2026-00-10T00:00Z"],
    ["day 00", "2026-10-00T00:00Z"],
    ["hour 25", "2026-10-18T25:00Z"],
    ["hour 24", "2026-10-18T24:00Z"],
    ["minute 60", "2026-10-18T22:60Z"],
    ["second 60", "2026-10-18T22:14:60Z"],
    ["an offset of 24 hours", "2026-10-18T22:14:00+24:00"],
    ["an offset of 60 minutes", "2026-10-18T22:14:00+02:60"],
    ["an instant before 2020", "2019-12-31T23:59:59Z"],
    ["2020 by its local date but 2019 in UTC", "2020-01-01T00:30:00+01:00"],
    ["more than 24 h ahead of the clock", "2026-10-20T12:00:01Z"],
    ["a year ahead", "2027-10-18T22:14:00Z"],
  ])("leaves out %s, which keeps the map live", (_, value) => {
    const parsed = parseHash(`#lat=1&t=${value}&z=5`, NOW);
    expect(parsed).toEqual({ lat: 1, z: 5 });
    expect("t" in parsed).toBe(false);
  });

  it("measures 'ahead' from the clock it is given, and from Date.now() by default", () => {
    const tomorrow = "2026-10-20T11:00:00Z";
    expect(parseHash(`#t=${tomorrow}`, NOW).t).toBe(Date.UTC(2026, 9, 20, 11));
    expect(parseHash(`#t=${tomorrow}`, NOW - DAY_MS).t).toBeUndefined();

    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW - DAY_MS);
    expect(parseHash(`#t=${tomorrow}`).t).toBeUndefined();
    vi.setSystemTime(NOW);
    expect(parseHash(`#t=${tomorrow}`).t).toBe(Date.UTC(2026, 9, 20, 11));
  });

  it("reads the page's own hash by default", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
    at(`/map#lat=34.85&lon=-82.39&z=10&t=${T_TEXT}&span=5`);
    expect(parseHash()).toEqual({ lat: 34.85, lon: -82.39, z: 10, t: T, span: 5 });
  });

  it("skips a value with a malformed escape, and only that key", () => {
    expect(() => parseHash("#lat=1&t=2026-10-18T22%3A14%3&hex=ab%ZZ&z=5&layers=%", NOW)).not.toThrow();
    expect(parseHash("#lat=1&t=2026-10-18T22%3A14%3&hex=ab%ZZ&z=5&layers=%", NOW)).toEqual({ lat: 1, z: 5 });
    expect(parseHash(`#hex=%E0%A4%A&t=${T_TEXT}`, NOW)).toEqual({ t: T });
  });

  it("reads the live keys as before", () => {
    expect(parseHash("#lat=52.1&lon=-0.5&z=9&hex=abc%20d&layers=clt&other=1&=x&bare", NOW))
      .toEqual({ lat: 52.1, lon: -0.5, z: 9, hex: "abc d", layers: "clt" });
    expect(parseHash("#hex=", NOW)).toEqual({ hex: null });
    expect(parseHash("", NOW)).toEqual({});
    expect(parseHash("#", NOW)).toEqual({});
  });
});

describe("parseHash: span", () => {
  it.each([
    ["5", 5],
    ["15", 15],
    ["1", 1],
    ["30", 30],
    ["2.4", 2],
    ["2.5", 3],
    ["0.4", 1],
    ["0", 1],
    ["-3", 1],
    ["45", 30],
    ["1e1", 10],
    ["%31%35", 15],
  ])("reads span=%s as %i", (value, expected) => {
    expect(parseHash(`#t=${T_TEXT}&span=${value}`, NOW)).toEqual({ t: T, span: expected });
  });

  it.each([["abc"], ["5min"], ["Infinity"], ["NaN"], ["1e400"], [""], ["%20"], ["%"]])(
    "leaves out span=%s",
    (value) => {
      const parsed = parseHash(`#t=${T_TEXT}&span=${value}`, NOW);
      expect(parsed).toEqual({ t: T });
      expect("span" in parsed).toBe(false);
    },
  );
});

describe("formatHashT", () => {
  it("writes UTC to the second, with Z", () => {
    expect(formatHashT(T)).toBe(T_TEXT);
    expect(formatHashT(Date.UTC(2026, 0, 2, 3, 4, 5))).toBe("2026-01-02T03:04:05Z");
  });

  it("floors to the second", () => {
    expect(formatHashT(T + 999)).toBe(T_TEXT);
    expect(formatHashT(T + 1000)).toBe("2026-10-18T22:14:01Z");
    expect(formatHashT(T - 1)).toBe("2026-10-18T22:13:59Z");
  });

  it.each([T, T + 1, T + 500, T + 999, T + 123_456, Date.UTC(2020, 0, 1)])(
    "round-trips %i through parseHash, floored to the second",
    (ms) => {
      expect(parseHash(`#t=${formatHashT(ms)}`, NOW).t).toBe(Math.floor(ms / 1000) * 1000);
    },
  );
});

describe("useHashWriter with the archive keys", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  const VIEW = { lat: 52, lon: -0.5, z: 9, hex: "abc123", layers: "clt" };
  const LIVE = "#lat=52.0000&lon=-0.5000&z=9&hex=abc123&layers=clt";

  function write(state: Parameters<ReturnType<typeof useHashWriter>>[0], between?: () => void) {
    const { result } = renderHook(() => useHashWriter());
    result.current(state);
    between?.();
    vi.advanceTimersByTime(250);
  }

  it("writes a live hash exactly as it always has", () => {
    at("/map");
    write(VIEW);
    expect(window.location.hash).toBe(LIVE);
  });

  it("drops a span that comes without an instant, as it always has", () => {
    at("/map#span=5");
    write(VIEW);
    expect(window.location.hash).toBe(LIVE);
  });

  it("drops an instant the hash holds that is not valid, as it always has", () => {
    at("/map#t=2027-10-18T22:14:00Z&span=5");
    write(VIEW);
    expect(window.location.hash).toBe(LIVE);
  });

  it("carries t and span over from the hash, after the view keys", () => {
    at(`/map#lat=1&lon=2&z=3&t=${T_TEXT}&span=5`);
    write(VIEW);
    expect(window.location.hash).toBe(`${LIVE}&t=${T_TEXT}&span=5`);
  });

  it("carries t alone when the hash has no span", () => {
    at(`/map#t=${T_TEXT}&lat=1`);
    write(VIEW);
    expect(window.location.hash).toBe(`${LIVE}&t=${T_TEXT}`);
  });

  it("carries them over in their canonical form", () => {
    at("/map#t=2026-10-19T00%3A14%2B02%3A00&span=7.6");
    write(VIEW);
    expect(window.location.hash).toBe(`${LIVE}&t=${T_TEXT}&span=8`);
  });

  it("carries what the hash holds when the write lands, not when it was queued", () => {
    at("/map");
    write(VIEW, () => writeArchiveHash(T, 5));
    expect(window.location.hash).toBe(`${LIVE}&t=${T_TEXT}&span=5`);

    write(VIEW, () => writeArchiveHash(null));
    expect(window.location.hash).toBe(LIVE);
  });

  it("writes t and span the caller gives in place of the hash's", () => {
    at(`/map#t=2026-10-01T00:00:00Z&span=5`);
    write({ ...VIEW, t: T + 999, span: 45 });
    expect(window.location.hash).toBe(`${LIVE}&t=${T_TEXT}&span=30`);
  });

  it("takes a given span with the hash's t", () => {
    at(`/map#t=${T_TEXT}&span=5`);
    write({ ...VIEW, span: 10 });
    expect(window.location.hash).toBe(`${LIVE}&t=${T_TEXT}&span=10`);
  });

  it("takes a given t with the hash's span", () => {
    at(`/map#t=2026-10-01T00:00:00Z&span=5`);
    write({ ...VIEW, t: T });
    expect(window.location.hash).toBe(`${LIVE}&t=${T_TEXT}&span=5`);
  });

  it("drops both when the caller gives t: null", () => {
    at(`/map#t=${T_TEXT}&span=5`);
    write({ ...VIEW, t: null, span: 10 });
    expect(window.location.hash).toBe(LIVE);
  });

  it("writes no span without an instant, even one the caller gives", () => {
    at("/map");
    write({ ...VIEW, span: 10 });
    expect(window.location.hash).toBe(LIVE);
  });
});

describe("writeArchiveHash", () => {
  it("sets t, and span when given, on a page with no hash", () => {
    at("/map");
    writeArchiveHash(T);
    expect(window.location.hash).toBe(`#t=${T_TEXT}`);
    at("/map");
    writeArchiveHash(T + 999, 5);
    expect(window.location.hash).toBe(`#t=${T_TEXT}&span=5`);
  });

  it("appends them after every other pair, each kept verbatim and in order", () => {
    at("/map?fleet=real#lat=1.5&foo=bar%20baz&bad=%E0%A4%A&flag&z=3");
    writeArchiveHash(T, 10);
    expect(window.location.search).toBe("?fleet=real");
    expect(window.location.hash).toBe(`#lat=1.5&foo=bar%20baz&bad=%E0%A4%A&flag&z=3&t=${T_TEXT}&span=10`);
  });

  it("replaces t where it stands, and keeps span when none is given", () => {
    at("/map#t=2026-10-01T00:00Z&lat=1&span=5&z=3");
    writeArchiveHash(T);
    expect(window.location.hash).toBe(`#t=${T_TEXT}&lat=1&span=5&z=3`);
  });

  it("replaces span where it stands, clamped", () => {
    at("/map#span=5&lat=1&t=2026-10-01T00:00Z");
    writeArchiveHash(T, 45);
    expect(window.location.hash).toBe(`#span=30&lat=1&t=${T_TEXT}`);
    writeArchiveHash(T, 0.2);
    expect(window.location.hash).toBe(`#span=1&lat=1&t=${T_TEXT}`);
  });

  it("collapses a repeated t into the first", () => {
    at("/map#t=a&lat=1&t=b&z=3");
    writeArchiveHash(T);
    expect(window.location.hash).toBe(`#t=${T_TEXT}&lat=1&z=3`);
  });

  it("removes t and span with t null, and nothing else", () => {
    at(`/map#lat=1&t=${T_TEXT}&foo=x%20y&span=5&z=3`);
    writeArchiveHash(null);
    expect(window.location.hash).toBe("#lat=1&foo=x%20y&z=3");
  });

  it("leaves no fragment once the archive keys were all it had", () => {
    at(`/map?fleet=real#t=${T_TEXT}&span=5`);
    writeArchiveHash(null, 10);
    expect(window.location.hash).toBe("");
    expect(window.location.href).toBe(`${window.location.origin}/map?fleet=real`);
  });

  it("keeps the router's history state on the entry it rewrites", () => {
    const routerState = { usr: null, key: "k1", idx: 2 };
    window.history.replaceState(routerState, "", "/map#lat=1");
    writeArchiveHash(T, 5);
    expect(window.location.hash).toBe(`#lat=1&t=${T_TEXT}&span=5`);
    expect(window.history.state).toEqual(routerState);
  });

  it("writes only when the hash changes", () => {
    at(`/map#lat=1&t=${T_TEXT}&span=5`);
    const replace = vi.spyOn(window.history, "replaceState");
    writeArchiveHash(T + 400, 5);
    writeArchiveHash(T);
    expect(replace).not.toHaveBeenCalled();
    at("/map#lat=1");
    replace.mockClear();
    writeArchiveHash(null);
    expect(replace).not.toHaveBeenCalled();
    writeArchiveHash(T);
    expect(replace).toHaveBeenCalledTimes(1);
  });

  it("ignores an instant that is not a finite number", () => {
    at(`/map#lat=1&t=${T_TEXT}`);
    writeArchiveHash(NaN, 5);
    writeArchiveHash(Infinity);
    expect(window.location.hash).toBe(`#lat=1&t=${T_TEXT}`);
  });
});

describe("useArchiveHash", () => {
  beforeEach(() => {
    // Only the clock: jsdom queues hashchange on timers of its own.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
  });

  /** A navigation to `hash`, as the address bar or a link makes one, waited
   *  out to its hashchange. The listener goes on after the hook's, so the
   *  hook's has run by the time it resolves. */
  async function navigate(hash: string) {
    const fired = new Promise<void>((resolve) => window.addEventListener("hashchange", () => resolve(), { once: true }));
    await act(async () => {
      window.location.hash = hash;
      await fired;
    });
  }

  it("starts from the hash, with span 15 when it names none", () => {
    at(`/map#lat=1&t=${T_TEXT}&span=5`);
    expect(renderHook(() => useArchiveHash()).result.current).toMatchObject({ t: T, span: 5, rev: 0 });
    at(`/map#t=${T_TEXT}`);
    expect(renderHook(() => useArchiveHash()).result.current).toMatchObject({ t: T, span: ARCHIVE_SPAN_DEFAULT_MIN, rev: 0 });
    at("/map#t=yesterday&span=3");
    expect(renderHook(() => useArchiveHash()).result.current).toMatchObject({ t: null, span: 3, rev: 0 });
    at("/map");
    expect(renderHook(() => useArchiveHash()).result.current).toMatchObject({ t: null, span: 15, rev: 0 });
  });

  it("takes a new t from a hashchange and bumps rev", async () => {
    at("/map#lat=1");
    const { result } = renderHook(() => useArchiveHash());
    await navigate(`#lat=1&t=${T_TEXT}`);
    expect(result.current).toMatchObject({ t: T, span: 15, rev: 1 });
    await navigate("#lat=1&t=2026-10-18T20:00:00Z&span=5");
    expect(result.current).toMatchObject({ t: Date.UTC(2026, 9, 18, 20), span: 5, rev: 2 });
  });

  it("bumps rev for a new span alone", async () => {
    at(`/map#t=${T_TEXT}`);
    const { result } = renderHook(() => useArchiveHash());
    await navigate(`#t=${T_TEXT}&span=5`);
    expect(result.current).toMatchObject({ t: T, span: 5, rev: 1 });
  });

  it("leaves rev alone for a hashchange that moves only the view", async () => {
    at(`/map#lat=1&t=${T_TEXT}`);
    const { result } = renderHook(() => useArchiveHash());
    await navigate(`#lat=2&t=${T_TEXT}`);
    await navigate(`#lat=2&lon=3&z=8&hex=abc&layers=cl&t=${T_TEXT}`);
    // The same instant and window, written differently.
    await navigate(`#lat=2&t=2026-10-19T00:14:00%2B02:00&span=15`);
    expect(result.current).toMatchObject({ t: T, span: 15, rev: 0 });
  });

  it("goes live when t is removed", async () => {
    at(`/map#lat=1&t=${T_TEXT}&span=5`);
    const { result } = renderHook(() => useArchiveHash());
    await navigate("#lat=1");
    expect(result.current).toMatchObject({ t: null, span: 15, rev: 1 });
  });

  it("reads the URL the navigation went to, not one written after it", async () => {
    at("/map");
    const { result } = renderHook(() => useArchiveHash());
    const fired = new Promise<void>((resolve) => window.addEventListener("hashchange", () => resolve(), { once: true }));
    await act(async () => {
      window.location.hash = `#t=${T_TEXT}`;
      // The map's own view write lands before the queued hashchange runs.
      window.history.replaceState(null, "", "/map#lat=1");
      await fired;
    });
    expect(result.current).toMatchObject({ t: T, rev: 1 });
  });

  it("compares with its own state when the event carries no URLs", () => {
    at("/map");
    const { result } = renderHook(() => useArchiveHash());
    at(`/map#t=${T_TEXT}`);
    act(() => { window.dispatchEvent(new HashChangeEvent("hashchange")); });
    expect(result.current).toMatchObject({ t: T, rev: 1 });
    act(() => { window.dispatchEvent(new HashChangeEvent("hashchange")); });
    expect(result.current).toMatchObject({ t: T, rev: 1 });
  });

  it("setArchive writes the hash and bumps rev, with no hashchange of its own", async () => {
    at("/map#lat=1");
    const { result } = renderHook(() => useArchiveHash());
    const setArchive = result.current.setArchive;
    const changes = vi.fn();
    window.addEventListener("hashchange", changes);

    act(() => result.current.setArchive(T, 5));
    expect(window.location.hash).toBe(`#lat=1&t=${T_TEXT}&span=5`);
    expect(result.current).toMatchObject({ t: T, span: 5, rev: 1 });

    act(() => result.current.setArchive(T + 60_000));
    expect(window.location.hash).toBe("#lat=1&t=2026-10-18T22:15:00Z&span=5");
    expect(result.current).toMatchObject({ t: T + 60_000, span: 5, rev: 2 });

    act(() => result.current.setArchive(T + 60_000, 99));
    expect(result.current).toMatchObject({ span: 30, rev: 3 });

    act(() => result.current.setArchive(null));
    expect(window.location.hash).toBe("#lat=1");
    expect(result.current).toMatchObject({ t: null, rev: 4 });

    // Let anything jsdom queued run.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    window.removeEventListener("hashchange", changes);
    expect(changes).not.toHaveBeenCalled();
    expect(result.current.rev).toBe(4);
    expect(result.current.setArchive).toBe(setArchive);
  });

  it("removes its listener on unmount", async () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    at("/map");
    const { result, unmount } = renderHook(() => useArchiveHash());
    const added = add.mock.calls.filter(([type]) => type === "hashchange");
    expect(added).toHaveLength(1);
    const handler = added[0][1];

    unmount();
    expect(remove).toHaveBeenCalledWith("hashchange", handler);

    add.mockRestore();
    remove.mockRestore();
    await navigate(`#t=${T_TEXT}`);
    expect(result.current).toMatchObject({ t: null, rev: 0 });
  });
});
