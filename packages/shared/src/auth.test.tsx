// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { useCurrentUser } from "./auth";

const ALICE = { id: "u1", email: "alice@offworldlab.com", name: "Alice", role: "user" };

function reply(status: number, body: unknown = null): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Drain the pending work the hook has queued, backoff sleeps included, without
 *  waiting the real ~9 s a full run of retries takes. */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000);
  });
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useCurrentUser", () => {
  it("resolves the signed-in user", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => reply(200, ALICE)));

    const { result } = renderHook(() => useCurrentUser());
    expect(result.current.loading).toBe(true);
    await settle();

    expect(result.current.user).toEqual(ALICE);
    expect(result.current.loading).toBe(false);
  });

  it("asks the server that one route", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async (_path: string) => reply(200, ALICE));
    vi.stubGlobal("fetch", fetchMock);

    renderHook(() => useCurrentUser());
    await settle();

    expect(fetchMock.mock.calls[0][0]).toBe("/api/auth/me");
  });

  it("settles as signed out on a 401, without retrying", async () => {
    // A 401 is an answer. The retries exist for a busy server, and spending
    // them here holds the login card behind a loading state for the length of
    // the backoff, which reads as a hung page.
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => reply(401, { detail: "Not authenticated" }));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCurrentUser());
    await settle();

    expect(result.current).toMatchObject({ user: null, loading: false });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries an answer that never came, and takes the one that does", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue(reply(200, ALICE));
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCurrentUser());
    await settle();

    expect(result.current.user).toEqual(ALICE);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("gives up after four attempts and settles as signed out", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useCurrentUser());
    await settle();

    expect(result.current).toMatchObject({ user: null, loading: false });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("holds the loading state across the backoff rather than flashing signed out", async () => {
    // Each failed attempt settling to `user: null` would bounce a guard to the
    // login page mid-retry, which is the outcome the retries exist to avoid.
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    const { result } = renderHook(() => useCurrentUser());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_000);
    });

    expect(result.current.loading).toBe(true);
  });

  it("waits longer for this answer than for an ordinary request", async () => {
    // The first answer after a boot is slow: the server may still be opening
    // the database. The 10 s a request times out at by default would call that
    // a failure and spend a retry on it.
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_path: string, init: RequestInit) =>
          new Promise<Response>((resolve, reject) => {
            init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
            setTimeout(() => resolve(reply(200, ALICE)), 25_000);
          }),
      ),
    );

    const { result } = renderHook(() => useCurrentUser());
    await settle();

    expect(result.current.user).toEqual(ALICE);
  });

  it("gives the long wait to the first attempt alone", async () => {
    // The wait is for a server still opening its database, which it has done
    // or not by the second ask. Spending it four times over holds a boot in
    // its loading state for two minutes against a server that hangs.
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_path: string, init: RequestInit) =>
          new Promise<Response>((_, reject) => {
            init.signal?.addEventListener("abort", () => reject(init.signal?.reason));
          }),
      ),
    );

    const { result } = renderHook(() => useCurrentUser());
    // 30 s for the first attempt, 10 s for each of the other three, and 9 s of
    // backoff between them: settled by 69 s, where four long waits would not be.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(80_000);
    });

    expect(result.current.loading).toBe(false);
  });

  it("stops retrying once the caller has gone", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = renderHook(() => useCurrentUser());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    unmount();
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("clears the user a caller signs out", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => reply(200, ALICE)));

    const { result } = renderHook(() => useCurrentUser());
    await settle();
    act(() => result.current.setUser(null));

    expect(result.current.user).toBeNull();
  });
});
