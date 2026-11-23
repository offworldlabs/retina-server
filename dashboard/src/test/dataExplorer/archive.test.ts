import { UnauthorizedError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { onSessionEnded } from "../../api/client";
import {
  cacheKey,
  type DayEntry,
  entryForScope,
  fetchDayListing,
  filePath,
  horizon,
  type Listed,
  LISTING_TIMEOUT_MS,
  PAGE,
  routeFor,
} from "../../pages/user/dataExplorer/archive";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({
  request: requestMock,
  UnauthorizedError: class UnauthorizedError extends Error {},
}));

const MODIFIED = "2026-09-17T14:00:00Z";

/** `n` listing rows for one day, all on one node. */
const rows = (n: number, node = "ret-a") =>
  Array.from({ length: n }, (_, i) => ({
    key: `year=2026/month=09/day=17/node_ref=${node}/part-${i}.parquet`,
    size_bytes: 100,
    modified: MODIFIED,
  }));

const entry = (over: Partial<DayEntry>): DayEntry => ({
  day: "2026-09-17",
  nodeRef: null,
  status: "done",
  files: [],
  ...over,
});

beforeEach(() => {
  requestMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("cacheKey", () => {
  it("distinguishes a whole-day listing from a node-scoped one", () => {
    expect(cacheKey("2026-09-17", null)).not.toBe(cacheKey("2026-09-17", "ret-a"));
  });
});

describe("fetchDayListing", () => {
  it("asks for the day as a slash-separated prefix", async () => {
    requestMock.mockResolvedValue({ files: [], total: 0 });
    await fetchDayListing("2026-09-17", null);
    expect(requestMock.mock.calls[0][0]).toContain("date=2026%2F09%2F17");
  });

  it("scopes to one node when given one", async () => {
    requestMock.mockResolvedValue({ files: [], total: 0 });
    await fetchDayListing("2026-09-17", "ret-a");
    expect(requestMock.mock.calls[0][0]).toContain("node_ref=ret-a");
  });

  it("omits node_ref when listing the whole day", async () => {
    requestMock.mockResolvedValue({ files: [], total: 0 });
    await fetchDayListing("2026-09-17", null);
    expect(requestMock.mock.calls[0][0]).not.toContain("node_ref");
  });

  it("keeps paging while `total` says there is more, even after a short page", async () => {
    // The route drops private nodes after paging, so a page can be short in
    // the middle of a list. Stopping here would truncate it.
    requestMock
      .mockResolvedValueOnce({ files: rows(10), total: 600 })
      .mockResolvedValueOnce({ files: rows(5), total: 600 });
    const files = await fetchDayListing("2026-09-17", null);
    expect(requestMock).toHaveBeenCalledTimes(2);
    expect(files).toHaveLength(15);
    expect(requestMock.mock.calls[1][0]).toContain(`offset=${PAGE}`);
  });

  it("stops on a short page when `total` is missing", async () => {
    requestMock.mockResolvedValue({ files: rows(10) });
    await fetchDayListing("2026-09-17", null);
    expect(requestMock).toHaveBeenCalledTimes(1);
  });

  it("drops rows whose key cannot be parsed", async () => {
    requestMock.mockResolvedValue({
      files: [...rows(2), { key: "junk", size_bytes: 1, modified: MODIFIED }],
      total: 3,
    });
    expect(await fetchDayListing("2026-09-17", null)).toHaveLength(2);
  });

  it("lets a failure out so the day can be shown as failed", async () => {
    requestMock.mockRejectedValue(new Error("HTTP 503"));
    await expect(fetchDayListing("2026-09-17", null)).rejects.toThrow("HTTP 503");
  });

  it("sends only the origin as the referrer, never the page's own address", async () => {
    requestMock.mockResolvedValue({ files: [], total: 0 });
    await fetchDayListing("2026-09-17", "ret-a");
    expect(requestMock.mock.calls[0][1]).toMatchObject({ referrerPolicy: "strict-origin" });
  });
});

describe("entryForScope", () => {
  it("answers any selection from a whole-day listing", () => {
    const cache = new Map([[cacheKey("2026-09-17", null), entry({})]]);
    expect(entryForScope(cache, "2026-09-17", new Set(["a", "b", "c"]))).not.toBeNull();
  });

  it("answers from a node-scoped listing only when that node is the whole selection", () => {
    const cache = new Map([
      [cacheKey("2026-09-17", "ret-a"), entry({ nodeRef: "ret-a" })],
    ]);
    expect(entryForScope(cache, "2026-09-17", new Set(["ret-a"]))).not.toBeNull();
    expect(entryForScope(cache, "2026-09-17", new Set(["ret-a", "ret-b"]))).toBeNull();
    expect(entryForScope(cache, "2026-09-17", new Set(["ret-b"]))).toBeNull();
  });

  it("is null for a day nothing has been loaded for", () => {
    expect(entryForScope(new Map(), "2026-09-17", new Set(["ret-a"]))).toBeNull();
  });
});

describe("horizon", () => {
  const days = ["2026-09-14", "2026-09-15", "2026-09-16"];
  const eff = new Set(["ret-a"]);
  const withFiles = (day: string) =>
    [cacheKey(day, null), entry({ day, files: [{ day } as never] })] as const;
  const empty = (day: string) => [cacheKey(day, null), entry({ day })] as const;

  it("names the oldest day with files when everything before it came back empty", () => {
    const cache = new Map([empty("2026-09-14"), withFiles("2026-09-15"), withFiles("2026-09-16")]);
    expect(horizon(cache, days, eff)).toBe("2026-09-15");
  });

  it("says nothing while an earlier day is still loading", () => {
    const cache = new Map([
      [cacheKey("2026-09-14", null), entry({ day: "2026-09-14", status: "loading" })],
      withFiles("2026-09-15"),
    ]);
    expect(horizon(cache, days, eff)).toBeNull();
  });

  it("says nothing when the whole range has files", () => {
    const cache = new Map([withFiles("2026-09-14"), withFiles("2026-09-15")]);
    expect(horizon(cache, days, eff)).toBeNull();
  });

  it("says nothing when no day has files at all", () => {
    const cache = new Map([empty("2026-09-14"), empty("2026-09-15"), empty("2026-09-16")]);
    expect(horizon(cache, days, eff)).toBeNull();
  });

  it("says nothing when an earlier day failed rather than came back empty", () => {
    // An errored day is unknown, not empty: claiming a horizon past it would
    // state as fact something no response established.
    const cache = new Map([
      [cacheKey("2026-09-14", null), entry({ day: "2026-09-14", status: "error", error: "HTTP 503" })],
      withFiles("2026-09-15"),
    ]);
    expect(horizon(cache, days, eff)).toBeNull();
  });
});

describe("the owner's archive", () => {
  it("is listed and paged from the owner's route with the public one's parameters", async () => {
    requestMock
      .mockResolvedValueOnce({ files: rows(10, "nderoof00000001"), total: 600 })
      .mockResolvedValueOnce({ files: rows(5, "nderoof00000001"), total: 600 });
    const files = await fetchDayListing("2026-09-17", "nderoof00000001", undefined, "owner");
    expect(requestMock.mock.calls.map((c) => c[0])).toEqual([
      "/api/auth/me/archive?date=2026%2F09%2F17&node_ref=nderoof00000001&limit=500&offset=0",
      `/api/auth/me/archive?date=2026%2F09%2F17&node_ref=nderoof00000001&limit=500&offset=${PAGE}`,
    ]);
    expect(requestMock.mock.calls[0][1]).toMatchObject({ timeoutMs: LISTING_TIMEOUT_MS });
    expect(files).toHaveLength(15);
    expect(files[0].node).toBe("nderoof00000001");
  });

  it("ends the session on the page at once when the owner's route answers 401", async () => {
    // The test document's page is the console's index, which a visitor may open,
    // so the client tells the auth context rather than leaving for the login.
    const told = vi.fn();
    const stop = onSessionEnded(told);
    requestMock.mockRejectedValue(new UnauthorizedError("Not authenticated"));
    await expect(fetchDayListing("2026-09-17", null, undefined, "owner")).rejects.toBeInstanceOf(UnauthorizedError);
    expect(told).toHaveBeenCalledTimes(1);
    stop();
  });

  it("serves each file from its own route, the key appended as issued", () => {
    const key = "year=2026/month=09/day=17/node_ref=nderoof00000001/part-0.parquet";
    expect(filePath(key, "owner")).toBe(`/api/auth/me/archive/${key}`);
    expect(filePath(key, "public")).toBe(`/api/data/archive/${key}`);
  });
});

describe("routeFor", () => {
  const listed = (pub: string[], owner: string[]): Listed => ({ public: new Set(pub), owner: new Set(owner) });

  it("takes the archive on screen when it listed the key", () => {
    expect(routeFor("k", listed(["k"], ["k"]), "owner")).toBe("owner");
    expect(routeFor("k", listed(["k"], ["k"]), "public")).toBe("public");
  });

  it("keeps a key only the owner's archive listed on the owner's route, whichever is on screen", () => {
    // A private node's file, ticked on the owner's view and still in the
    // basket on the network's, which never served it.
    expect(routeFor("k", listed([], ["k"]), "public")).toBe("owner");
  });

  it("prefers the public route for a key only it listed, which needs no session", () => {
    expect(routeFor("k", listed(["k"], []), "owner")).toBe("public");
  });

  it("goes to the archive on screen for a key neither has listed", () => {
    expect(routeFor("k", listed([], []), "owner")).toBe("owner");
    expect(routeFor("k", listed([], []), "public")).toBe("public");
  });
});
