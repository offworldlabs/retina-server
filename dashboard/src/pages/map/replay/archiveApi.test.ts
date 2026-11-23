import { HttpError, UnauthorizedError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../../../api/client";

const realLocation = window.location;

/** A page that needs a session, where the console's own wrapper would send a 401 to the login page. */
function stubLocation(pathname: string, hostname = "app.retina.fm") {
  const loc = { pathname, hostname, search: "", href: `https://${hostname}${pathname}` };
  Object.defineProperty(window, "location", { value: loc, writable: true, configurable: true });
  return loc;
}

const fetchMock = vi.fn();

function answer(status: number, body: unknown) {
  fetchMock.mockImplementation(async () => new Response(JSON.stringify(body), { status }));
}

/** The URL and init of the one fetch the last call made. */
function sent(): [URL, RequestInit] {
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  return [new URL(path, "https://app.retina.fm"), init];
}

/** A fetch that never answers and gives up only when its signal aborts. */
function hang() {
  fetchMock.mockImplementation(
    (_path: string, init: RequestInit) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
      }),
  );
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
});

const FROM = Date.UTC(2026, 9, 18, 19, 45);
const TO = Date.UTC(2026, 9, 18, 20, 15);

describe("api.archiveTracks", () => {
  it("asks /api/data/tracks for the window in integer epoch ms, real fleet by default", async () => {
    answer(200, { from: FROM, to: TO, fleet: "real", count: 0, truncated: false, rows: [] });
    const resp = await api.archiveTracks({ from: FROM + 0.7, to: TO + 0.2 });
    const [url, init] = sent();
    expect(url.pathname).toBe("/api/data/tracks");
    expect([...url.searchParams]).toEqual([
      ["from", String(FROM)],
      ["to", String(TO)],
      ["fleet", "real"],
    ]);
    expect(init.method ?? "GET").toBe("GET");
    expect(init.referrerPolicy).toBe("strict-origin");
    expect(init.credentials).toBe("same-origin");
    expect(resp).toEqual({ from: FROM, to: TO, fleet: "real", count: 0, truncated: false, rows: [] });
  });

  it("keeps the origin-only referrer whatever the caller passes, and honours its signal", async () => {
    hang();
    const controller = new AbortController();
    const call = api.archiveTracks(
      { from: FROM, to: TO, fleet: "real" },
      { signal: controller.signal, referrerPolicy: "unsafe-url" },
    );
    const [, init] = sent();
    expect(init.referrerPolicy).toBe("strict-origin");
    controller.abort();
    await expect(call).rejects.toMatchObject({ name: "AbortError" });
  });

  it("waits 30 s by default before timing out", async () => {
    vi.useFakeTimers();
    hang();
    const settled = vi.fn();
    const call = api.archiveTracks({ from: FROM, to: TO }).catch((e) => {
      settled(e);
      throw e;
    });
    call.catch(() => {});
    await vi.advanceTimersByTimeAsync(29_999);
    expect(settled).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await expect(call).rejects.toMatchObject({ name: "TimeoutError" });
  });

  it("takes the caller's timeout instead", async () => {
    vi.useFakeTimers();
    hang();
    const call = api.archiveTracks({ from: FROM, to: TO }, { timeoutMs: 5_000 });
    call.catch(() => {});
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(call).rejects.toMatchObject({ name: "TimeoutError" });
  });

  it("rejects a refusal with the HttpError carrying the route's detail", async () => {
    answer(422, { detail: "window exceeds the 60-minute cap" });
    const err = await api.archiveTracks({ from: FROM, to: FROM + 61 * 60_000 }).catch((e) => e);
    expect(err).toBeInstanceOf(HttpError);
    expect(err).toMatchObject({ status: 422, message: "window exceeds the 60-minute cap" });
  });

  it("leaves a visitor where they are on a 401, as a public route should", async () => {
    // On a page that needs a session the console's wrapper would navigate to /login.
    const loc = stubLocation("/nodes/ret-0042");
    const before = loc.href;
    answer(401, null);
    await expect(api.archiveTracks({ from: FROM, to: TO })).rejects.toBeInstanceOf(UnauthorizedError);
    expect(loc.href).toBe(before);
  });
});

describe("api.trackAvailability", () => {
  it("asks /api/data/tracks/availability for UTC days, real fleet by default", async () => {
    const body = { from: "2026-10-15", to: "2026-10-19", fleet: "real", hours: [], total: 0 };
    answer(200, body);
    const resp = await api.trackAvailability({ from: "2026-10-15", to: "2026-10-19" });
    const [url, init] = sent();
    expect(url.pathname).toBe("/api/data/tracks/availability");
    expect([...url.searchParams]).toEqual([
      ["from", "2026-10-15"],
      ["to", "2026-10-19"],
      ["fleet", "real"],
    ]);
    expect(init.referrerPolicy).toBe("strict-origin");
    expect(resp).toEqual(body);
  });

  it("waits 15 s by default before timing out", async () => {
    vi.useFakeTimers();
    hang();
    const settled = vi.fn();
    const call = api.trackAvailability({ from: "2026-10-15", to: "2026-10-19" }).catch((e) => {
      settled(e);
      throw e;
    });
    call.catch(() => {});
    await vi.advanceTimersByTimeAsync(14_999);
    expect(settled).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await expect(call).rejects.toMatchObject({ name: "TimeoutError" });
  });

  it("keeps the origin-only referrer and the caller's timeout", async () => {
    vi.useFakeTimers();
    hang();
    const call = api.trackAvailability(
      { from: "2026-10-15", to: "2026-10-19", fleet: "real" },
      { timeoutMs: 2_000, referrerPolicy: "no-referrer-when-downgrade" },
    );
    call.catch(() => {});
    expect(fetchMock.mock.calls[0][1].referrerPolicy).toBe("strict-origin");
    await vi.advanceTimersByTimeAsync(2_000);
    await expect(call).rejects.toMatchObject({ name: "TimeoutError" });
  });

  it("leaves a visitor where they are on a 401", async () => {
    const loc = stubLocation("/account", "app.retina.fm");
    const before = loc.href;
    answer(401, null);
    await expect(api.trackAvailability({ from: "2026-10-15", to: "2026-10-19" })).rejects.toBeInstanceOf(
      UnauthorizedError,
    );
    expect(loc.href).toBe(before);
  });
});

describe("api.archiveAdsb", () => {
  const A_FROM = Date.UTC(2026, 9, 18, 19, 30);
  const A_TO = A_FROM + 3_600_000;
  const body = { from: A_FROM, to: A_TO, fleet: "real", count: 0, truncated: false, rows: [] };

  it("asks /api/data/adsb for the window in integer epoch ms, real fleet, no bbox, origin-only referrer", async () => {
    answer(200, body);
    const resp = await api.archiveAdsb({ from: A_FROM + 0.7, to: A_TO + 0.2 });
    const [url, init] = sent();
    expect(url.pathname).toBe("/api/data/adsb");
    expect([...url.searchParams]).toEqual([
      ["from", String(A_FROM)],
      ["to", String(A_TO)],
      ["fleet", "real"],
    ]);
    expect(init.referrerPolicy).toBe("strict-origin");
    expect(resp).toEqual(body);
  });

  it("waits 30 s by default before timing out", async () => {
    vi.useFakeTimers();
    hang();
    const settled = vi.fn();
    const call = api.archiveAdsb({ from: A_FROM, to: A_TO }).catch((e) => {
      settled(e);
      throw e;
    });
    call.catch(() => {});
    await vi.advanceTimersByTimeAsync(29_999);
    expect(settled).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await expect(call).rejects.toMatchObject({ name: "TimeoutError" });
  });
});

describe("api.archiveDetections", () => {
  const D_FROM = Date.UTC(2026, 9, 18, 19, 57);
  const D_TO = D_FROM + 600_000;
  const body = { from: D_FROM, to: D_TO, fleet: "real", count: 0, truncated: false, local: true, rows: [] };

  it("asks /api/data/detections for the window in integer epoch ms, real fleet, no bbox, origin-only referrer", async () => {
    answer(200, body);
    const resp = await api.archiveDetections({ from: D_FROM + 0.7, to: D_TO + 0.2 });
    const [url, init] = sent();
    expect(url.pathname).toBe("/api/data/detections");
    expect([...url.searchParams]).toEqual([
      ["from", String(D_FROM)],
      ["to", String(D_TO)],
      ["fleet", "real"],
    ]);
    expect(init.referrerPolicy).toBe("strict-origin");
    expect(resp).toEqual(body);
  });

  it("sends a bbox as south,west,north,east when given, and honours the caller's signal", async () => {
    hang();
    const controller = new AbortController();
    const call = api.archiveDetections(
      { from: D_FROM, to: D_TO, fleet: "real", bbox: [34.5, -83, 35.5, -82] },
      { signal: controller.signal },
    );
    const [url] = sent();
    expect(url.searchParams.get("bbox")).toBe("34.5,-83,35.5,-82");
    controller.abort();
    await expect(call).rejects.toMatchObject({ name: "AbortError" });
  });
});
