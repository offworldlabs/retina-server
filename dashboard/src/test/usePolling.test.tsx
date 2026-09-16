import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useFetch, usePolling } from "../hooks/usePolling";

/** A promise the test settles by hand, so an in-flight state can be observed. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Lets the settled promises inside the hook reach setState. */
const flush = () => act(async () => {});

const tick = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

describe("usePolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("fetches on mount and is loading until the result lands", async () => {
    const fetcher = vi.fn().mockResolvedValue("first");
    const { result } = renderHook(() => usePolling(fetcher, 5000));

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.loading).toBe(true);
    expect(result.current.pending).toBe(true);
    expect(result.current.data).toBeNull();
    expect(result.current.updatedAt).toBeNull();

    await flush();

    expect(result.current.loading).toBe(false);
    expect(result.current.pending).toBe(false);
    expect(result.current.data).toBe("first");
    expect(result.current.error).toBeNull();
    expect(result.current.updatedAt).toBeInstanceOf(Date);
  });

  it("fetches again every interval, and a timed refresh is neither loading nor pending", async () => {
    const second = deferred<string>();
    const fetcher = vi.fn().mockResolvedValueOnce("first").mockReturnValueOnce(second.promise);
    const { result } = renderHook(() => usePolling(fetcher, 5000));
    await flush();

    await tick(4999);
    expect(fetcher).toHaveBeenCalledTimes(1);
    await tick(1);
    expect(fetcher).toHaveBeenCalledTimes(2);

    // The page keeps what it has on screen while the refresh is in flight.
    expect(result.current.data).toBe("first");
    expect(result.current.loading).toBe(false);
    expect(result.current.pending).toBe(false);

    await act(async () => second.resolve("second"));
    expect(result.current.data).toBe("second");
  });

  it("keeps the last data and reports a failed refresh, then clears it on the next success", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce("first")
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValueOnce("third");
    const { result } = renderHook(() => usePolling(fetcher, 5000));
    await flush();

    await tick(5000);
    expect(result.current.data).toBe("first");
    expect(result.current.error?.message).toBe("boom");
    expect(result.current.loading).toBe(false);
    expect(console.error).toHaveBeenCalledWith(expect.any(Error));

    await tick(5000);
    expect(result.current.data).toBe("third");
    expect(result.current.error).toBeNull();
  });

  it("ends loading on a failed first request", async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error("down"));
    const { result } = renderHook(() => usePolling(fetcher, 5000));
    await flush();

    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBeNull();
    expect(result.current.error?.message).toBe("down");
  });

  it("wraps a rejection that is not an Error", async () => {
    const fetcher = vi.fn().mockRejectedValue("nope");
    const { result } = renderHook(() => usePolling(fetcher, 5000));
    await flush();

    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toBe("nope");
  });

  it("refetches at once when the key changes, pending but not loading, and restarts the schedule", async () => {
    const fetcher = vi.fn(async (key: string) => `${key}-result`);
    const { result, rerender } = renderHook(
      ({ key }) => usePolling(() => fetcher(key), 5000, key),
      { initialProps: { key: "a" } },
    );
    await flush();
    expect(result.current.data).toBe("a-result");

    await tick(3000);
    const second = deferred<string>();
    fetcher.mockReturnValueOnce(second.promise);
    rerender({ key: "b" });

    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenLastCalledWith("b");
    expect(result.current.pending).toBe(true);
    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBe("a-result");

    await act(async () => second.resolve("b-result"));
    expect(result.current.pending).toBe(false);
    expect(result.current.data).toBe("b-result");

    // The old schedule would have fired 2 s from here; the new one fires 5 s
    // from the key change.
    await tick(2000);
    expect(fetcher).toHaveBeenCalledTimes(2);
    await tick(3000);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("refresh() fetches now and is pending until that request settles", async () => {
    const later = deferred<string>();
    const fetcher = vi.fn().mockResolvedValueOnce("first").mockReturnValueOnce(later.promise);
    const { result } = renderHook(() => usePolling(fetcher, 5000));
    await flush();

    act(() => result.current.refresh());
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(result.current.pending).toBe(true);
    expect(result.current.loading).toBe(false);

    await act(async () => later.resolve("second"));
    expect(result.current.pending).toBe(false);
    expect(result.current.data).toBe("second");
  });

  it("uses the latest fetcher on the next tick without restarting the schedule", async () => {
    const a = vi.fn().mockResolvedValue("a");
    const b = vi.fn().mockResolvedValue("b");
    const { result, rerender } = renderHook(({ f }) => usePolling(f, 5000), {
      initialProps: { f: a },
    });
    await flush();

    await tick(3000);
    rerender({ f: b });
    expect(b).not.toHaveBeenCalled();

    await tick(2000);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe("b");
  });

  it("stops the schedule on unmount", async () => {
    const fetcher = vi.fn().mockResolvedValue("first");
    const { unmount } = renderHook(() => usePolling(fetcher, 5000));
    await flush();

    unmount();
    await tick(20000);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each(["success", "failure"])("ignores an older request's late %s", async (outcome) => {
    const older = deferred<string>();
    const newer = deferred<string>();
    const fetcher = vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    const { result } = renderHook(() => usePolling(fetcher, 5000));
    await tick(5000);
    await act(async () => newer.resolve("newer"));
    const updatedAt = result.current.updatedAt;

    await act(async () => {
      if (outcome === "success") older.resolve("older");
      else older.reject(new Error("old failure"));
    });

    expect(result.current.data).toBe("newer");
    expect(result.current.error).toBeNull();
    expect(result.current.updatedAt).toBe(updatedAt);
  });

  it("ignores an outstanding response after changing keys", async () => {
    const older = deferred<string>();
    const fetcher = vi.fn().mockReturnValueOnce(older.promise).mockResolvedValueOnce("b");
    const { result, rerender } = renderHook(({ key }) => usePolling(fetcher, 5000, key), {
      initialProps: { key: "a" },
    });
    rerender({ key: "b" });
    await flush();
    await act(async () => older.resolve("a"));
    expect(result.current.data).toBe("b");
  });

  it("publishes a slow result while a newer request is still pending", async () => {
    const older = deferred<string>();
    const newer = deferred<string>();
    const fetcher = vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);
    const { result } = renderHook(() => usePolling(fetcher, 5000));
    await tick(5000);
    await act(async () => older.resolve("older"));
    expect(result.current.data).toBe("older");
    await act(async () => newer.resolve("newer"));
    expect(result.current.data).toBe("newer");
  });
});

describe("useFetch", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("fetches once and never on a timer", async () => {
    const fetcher = vi.fn().mockResolvedValue("only");
    const { result } = renderHook(() => useFetch(fetcher));
    await flush();

    expect(result.current.data).toBe("only");
    await tick(60_000);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("fetches again when the key changes", async () => {
    const fetcher = vi.fn(async (page: number) => ({ page }));
    const { result, rerender } = renderHook(({ page }) => useFetch(() => fetcher(page), page), {
      initialProps: { page: 0 },
    });
    await flush();
    expect(result.current.data).toEqual({ page: 0 });

    rerender({ page: 1 });
    expect(result.current.pending).toBe(true);
    await flush();
    expect(result.current.data).toEqual({ page: 1 });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});
