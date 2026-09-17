import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  cacheKey,
  type DayEntry,
  entryForScope,
  fetchDayListing,
  horizon,
  PAGE,
} from "../../pages/user/dataExplorer/archive";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({ request: requestMock }));

const MODIFIED = "2026-09-17T14:00:00Z";

/** `n` listing rows for one day, all on one node. */
const rows = (n: number, node = "ret-a") =>
  Array.from({ length: n }, (_, i) => ({
    key: `year=2026/month=09/day=17/node_id=${node}/part-${i}.parquet`,
    size_bytes: 100,
    modified: MODIFIED,
  }));

const entry = (over: Partial<DayEntry>): DayEntry => ({
  day: "2026-09-17",
  nodeId: null,
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
    expect(requestMock.mock.calls[0][0]).toContain("node_id=ret-a");
  });

  it("omits node_id when listing the whole day", async () => {
    requestMock.mockResolvedValue({ files: [], total: 0 });
    await fetchDayListing("2026-09-17", null);
    expect(requestMock.mock.calls[0][0]).not.toContain("node_id");
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
});

describe("entryForScope", () => {
  it("answers any selection from a whole-day listing", () => {
    const cache = new Map([[cacheKey("2026-09-17", null), entry({})]]);
    expect(entryForScope(cache, "2026-09-17", new Set(["a", "b", "c"]))).not.toBeNull();
  });

  it("answers from a node-scoped listing only when that node is the whole selection", () => {
    const cache = new Map([
      [cacheKey("2026-09-17", "ret-a"), entry({ nodeId: "ret-a" })],
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
