import { act, renderHook, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";
import { MAX_INFLIGHT, useArchiveScan } from "../../pages/user/dataExplorer/useArchiveScan";

const { fetchMock } = vi.hoisted(() => ({ fetchMock: vi.fn() }));
vi.mock("../../pages/user/dataExplorer/archive", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchDayListing: fetchMock,
}));

const file = (day: string, node = "ret-a"): ArchiveFile => ({
  key: `${day}/${node}/part.parquet`,
  name: "part.parquet",
  node,
  day,
  size: 10,
  endMs: Date.parse(`${day}T01:00:00Z`),
  startMs: Date.parse(`${day}T00:00:00Z`),
});

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (r: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  fetchMock.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useArchiveScan", () => {
  it("lists every day in the range", async () => {
    fetchMock.mockImplementation((day: string) => Promise.resolve([file(day)]));
    const days = ["2026-09-15", "2026-09-16"];
    const { result } = renderHook(() => useArchiveScan(days, null));

    await waitFor(() => expect(result.current.files.size).toBe(2));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("holds no more than three listings in flight", async () => {
    const gates = Array.from({ length: 5 }, () => deferred<ArchiveFile[]>());
    let issued = 0;
    fetchMock.mockImplementation(() => gates[issued++].promise);

    const days = ["2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"];
    renderHook(() => useArchiveScan(days, null));

    await waitFor(() => expect(issued).toBe(MAX_INFLIGHT));
    await act(async () => gates[0].resolve([]));
    await waitFor(() => expect(issued).toBe(MAX_INFLIGHT + 1));
  });

  it("scopes to one node when exactly one is selected", async () => {
    fetchMock.mockResolvedValue([]);
    renderHook(() => useArchiveScan(["2026-09-17"], new Set(["ret-a"])));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][1]).toBe("ret-a");
  });

  it("lists the whole day when nothing has been selected", async () => {
    fetchMock.mockResolvedValue([file("2026-09-17", "ret-a")]);
    const { result } = renderHook(() => useArchiveScan(["2026-09-17"], null));
    await waitFor(() => expect(result.current.nodeIds.size).toBe(1));
    // Exactly one node has now been discovered. That is a fact about what has
    // loaded, not a selection of one, and must not have narrowed the request.
    expect(fetchMock.mock.calls[0][1]).toBeNull();
  });

  it("does not list the day again when one node is discovered", async () => {
    fetchMock.mockResolvedValue([file("2026-09-17", "ret-a")]);
    const { result } = renderHook(() => useArchiveScan(["2026-09-17"], null));
    await waitFor(() => expect(result.current.nodeIds.size).toBe(1));
    await act(async () => {});
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reuses a whole-day listing when the selection narrows to one node", async () => {
    fetchMock.mockResolvedValue([file("2026-09-17", "ret-a")]);
    const { rerender } = renderHook(({ sel }) => useArchiveScan(["2026-09-17"], sel), {
      initialProps: { sel: null as Set<string> | null },
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender({ sel: new Set(["ret-a"]) });
    await act(async () => {});
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("records a failed day and lists it again on retry", async () => {
    fetchMock.mockRejectedValueOnce(new Error("HTTP 503"));
    const { result } = renderHook(() => useArchiveScan(["2026-09-17"], null));

    await waitFor(() => {
      const entry = result.current.entries.get("2026-09-17|*");
      expect(entry.status).toBe("error");
      expect(entry.error).toContain("HTTP 503");
    });

    fetchMock.mockResolvedValueOnce([file("2026-09-17")]);
    act(() => result.current.retry("2026-09-17"));
    await waitFor(() =>
      expect(result.current.entries.get("2026-09-17|*").status).toBe("done"),
    );
  });

  it("collects node ids from the keys it sees", async () => {
    fetchMock.mockResolvedValue([file("2026-09-17", "ret-a"), file("2026-09-17", "ret-b")]);
    const { result } = renderHook(() => useArchiveScan(["2026-09-17"], null));
    await waitFor(() => expect(result.current.nodeIds).toEqual(new Set(["ret-a", "ret-b"])));
  });

  it("keeps files it has already seen when the range moves off them", async () => {
    fetchMock.mockImplementation((day: string) => Promise.resolve([file(day)]));
    const { result, rerender } = renderHook(({ days }) => useArchiveScan(days, null), {
      initialProps: { days: ["2026-09-16"] },
    });
    await waitFor(() => expect(result.current.files.size).toBe(1));

    rerender({ days: ["2026-09-17"] });
    await waitFor(() => expect(result.current.files.size).toBe(2));
  });

  it("converges when React double-invokes the mount", async () => {
    fetchMock.mockImplementation((day: string) => Promise.resolve([file(day)]));
    const { result } = renderHook(() => useArchiveScan(["2026-09-17"], null), {
      wrapper: ({ children }) => <StrictMode>{children}</StrictMode>,
    });

    await waitFor(() => expect(result.current.entries.get("2026-09-17|*").status).toBe("done"));
    expect(result.current.files.size).toBe(1);
    expect(result.current.nodeIds).toEqual(new Set(["ret-a"]));
  });

  it("refetches the whole day when the selection widens back to everything", async () => {
    fetchMock.mockResolvedValue([file("2026-09-17", "ret-a")]);
    const { rerender } = renderHook(({ sel }) => useArchiveScan(["2026-09-17"], sel), {
      initialProps: { sel: new Set(["ret-a"]) as Set<string> | null },
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][1]).toBe("ret-a");

    // One node has been seen, but nothing is selected any more, so the
    // scoped listing cannot answer for the whole fleet.
    rerender({ sel: null });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][1]).toBeNull();
  });
});
